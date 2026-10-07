"""Exercise setup bootstrap boundaries without modifying the user's Conda."""
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SETUP = (PROJECT / "scripts/setup.sh").read_text().removesuffix('main "$@"\n')
INSTALLER = """#!/usr/bin/env bash
set -eu
[ "$1" = -b ] && [ "$2" = -p ]
mkdir -p "$3/bin"
cp "$FAKE_CONDA" "$3/bin/conda"
"""
CONDA = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
    info) dirname "$(dirname "$0")" ;;
    env) touch "$ENV_CREATED" ;;
    run)
        if [ "$6" = --version ]; then [ -f "$ENV_CREATED" ]; fi
        ;;
    *) exit 1 ;;
esac
"""


class SetupTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / "tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.prefix = self.root / "miniforge3"
        self.payload = self.root / "installer.sh"
        self.payload.write_text(INSTALLER)
        self.fake_conda = self.root / "fake-conda"
        self.fake_conda.write_text(CONDA)
        self.fake_conda.chmod(0o755)
        self.calls = self.root / "calls"
        self.env_created = self.root / "environment-created"
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin",
                        FAKE_CONDA=str(self.fake_conda), CALLS=str(self.calls),
                        ENV_CREATED=str(self.env_created), PAYLOAD=str(self.payload))
        self.executable("curl", """while [ "$1" != --output ]; do shift; done
cp "$PAYLOAD" "$2"
""")

    def executable(self, name, body):
        target = self.bin / name
        target.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        target.chmod(0o755)

    def run_shell(self, body, *, valid_checksum=True):
        checksum = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        overrides = f"PROJECT_DIR={shlex.quote(str(self.root))}\n"
        if valid_checksum:
            overrides += f"MINIFORGE_SHA256={checksum}\n"
        script = self.root / "test-shell.sh"
        script.write_text(SETUP + overrides + body)
        return subprocess.run(["bash", str(script)],
                              env=self.env, text=True, capture_output=True)

    def install(self, **kwargs):
        return self.run_shell(f"install_miniforge {shlex.quote(str(self.prefix))}\n", **kwargs)

    def assert_download_cleaned(self):
        self.assertEqual(list((self.root / "tmp").glob("miniforge-install.*")), [])

    def test_verified_batch_install_and_cleanup(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "bin/conda").is_file())
        self.assert_download_cleaned()

    def test_wget_download_when_curl_unavailable(self):
        self.executable("wget", """for arg in "$@"; do
    case "$arg" in --output-document=*) cp "$PAYLOAD" "${arg#*=}" ;; esac
done
""")
        # Hide the system curl only from the availability probe.
        body = """command() {
    if [ "$*" = '-v curl' ]; then return 1; fi
    builtin command "$@"
}
"""
        result = self.run_shell(body + f"install_miniforge {shlex.quote(str(self.prefix))}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "bin/conda").is_file())
        self.assert_download_cleaned()

    def test_corrupt_download_is_never_executed(self):
        result = self.install(valid_checksum=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256 mismatch", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assert_download_cleaned()

    def test_download_failure_and_cleanup(self):
        self.executable("curl", "exit 22\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download failed", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assert_download_cleaned()

    def test_existing_directory_is_preserved(self):
        self.prefix.mkdir()
        marker = self.prefix / "keep"
        marker.write_text("existing installation")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(marker.read_text(), "existing installation")

    def test_missing_conda_installs_then_creates_named_environment(self):
        # Redirect standard installation candidates without changing HOME.
        body = "declare -f find_or_install_conda"
        definition = self.run_shell(body).stdout.replace("$HOME/", str(self.root) + "/")
        result = self.run_shell(definition + "\ncreate_or_check_environment\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.env_created.exists())
        calls = self.calls.read_text()
        self.assertIn("env create -f", calls)
        self.assertIn("run --no-capture-output -n nautilus-benchmark python -m pip install", calls)
        self.assertNotIn("benchmark.py", calls)
        self.assert_download_cleaned()

    def test_installer_failure_and_cleanup(self):
        self.payload.write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installation failed", result.stderr)
        self.assert_download_cleaned()

    def test_full_setup_reports_activation_and_does_not_run_benchmark(self):
        (self.bin / "conda").symlink_to(self.fake_conda)
        (self.root / "results").mkdir()
        data = PROJECT / "data/btc-perp-20211231-20220201_1m.csv"
        result = self.run_shell(f"DATA_FILE={shlex.quote(str(data))}\nmain\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[成功]", result.stdout)
        self.assertIn("source ", result.stdout)
        self.assertIn("/etc/profile.d/conda.sh", result.stdout)
        self.assertIn("Benchmark executed: NO", result.stdout)
        self.assertNotIn("benchmark.py", self.calls.read_text())

    def test_existing_conda_and_environment_skip_install_and_create(self):
        (self.bin / "conda").symlink_to(self.fake_conda)
        self.env_created.touch()
        self.executable("curl", "exit 99\n")
        result = self.run_shell("create_or_check_environment\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("env create", self.calls.read_text())
        self.assertFalse(self.prefix.exists())
        pip_call = next(line for line in self.calls.read_text().splitlines()
                        if "python -m pip install" in line)
        self.assertIn("--only-binary=:all:", pip_call)
        self.assertIn("releases/download/v2.0.0rc6/nautilus_trader-2.0.0rc6-", pip_call)
        self.assertIn("#sha256=9b4002a7bf5e6399c51073039b740ccf3ca7a1e2584ff72c7d479f03eaa9658d", pip_call)

    def test_release_pin_matches_benchmark_and_snapshot(self):
        version = re.search(r"^NAUTILUS_VERSION=(\S+)$", SETUP, re.MULTILINE).group(1)
        benchmark = (PROJECT / "scripts/benchmark.py").read_text()
        expected = re.search(r'^EXPECTED_NAUTILUS_VERSION = "([^"]+)"$',
                             benchmark, re.MULTILINE).group(1)
        snapshot = (PROJECT / "requirements/pip-freeze.txt").read_text().splitlines()
        self.assertEqual(version, expected)
        self.assertIn(f"nautilus-trader=={version}", snapshot)


if __name__ == "__main__":
    unittest.main()
