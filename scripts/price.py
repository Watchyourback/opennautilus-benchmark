#!/usr/bin/env python3
"""把本机价格折算为美元/月：环境变量、本机记录，或交互询问。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping

from score import ScoreError

ROOT = Path(__file__).resolve().parent.parent
LOCAL_RECORD = ROOT / ".benchmark-local.json"
RATE_URL = "https://api.frankfurter.dev/v1/latest?base={currency}&symbols=USD"
CURRENCY_ALIASES = {"RMB": "CNY", "元": "CNY", "$": "USD", "€": "EUR", "美元": "USD", "欧元": "EUR", "人民币": "CNY"}


def parse_amount(raw: str) -> float:
    try:
        value = float(raw.strip().replace(",", ""))
    except ValueError:
        raise ScoreError(f"价格不是数字: {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ScoreError(f"价格必须大于 0: {raw!r}")
    return value


def normalize_currency(raw: str) -> str:
    code = CURRENCY_ALIASES.get(raw.strip(), raw.strip().upper())
    code = CURRENCY_ALIASES.get(code, code)
    if len(code) != 3 or not code.isalpha():
        raise ScoreError(f"无法识别的币种: {raw!r}（请用 USD / EUR / CNY 这类三字母代码，RMB 也可以）")
    return code


def fetch_usd_rate(currency: str, timeout: float = 10.0) -> tuple[float, str]:
    """联网查 1 单位币种 = ? 美元，返回 (汇率, 汇率日期)。数据源 Frankfurter（欧洲央行日汇率）。"""
    if currency == "USD":
        return 1.0, "-"
    try:
        # 默认的 Python-urllib User-Agent 会被该服务 403 拒绝，需要自定义。
        request = urllib.request.Request(
            RATE_URL.format(currency=currency), headers={"User-Agent": "opennautilus-benchmark/1.0"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return float(payload["rates"]["USD"]), str(payload["date"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        raise ScoreError(f"获取 {currency}→USD 汇率失败: {error}") from error


def to_monthly_usd(
    amount: float, currency: str, period: str, fetch_rate: Callable[[str], tuple[float, str]] = fetch_usd_rate
) -> tuple[float, dict[str, Any]]:
    rate, rate_date = fetch_rate(currency)
    monthly = amount * rate / (12 if period == "y" else 1)
    return monthly, {"amount": amount, "currency": currency, "period": period, "rate": rate, "rate_date": rate_date}


def machine_fingerprint() -> str:
    """机器指纹的哈希；整目录复制到另一台服务器后指纹会变，价格记录随之失效。"""
    try:
        raw = Path("/etc/machine-id").read_text().strip()
    except OSError:
        raw = ""
    return hashlib.sha256((raw or platform.node()).encode()).hexdigest()


def monthly_from_source(source: Mapping[str, Any]) -> float:
    """用当时保存的金额、周期和汇率重算美元/月，避免月费字段和输入不一致。"""
    amount = float(source["amount"])
    rate = float(source.get("rate", 1.0))
    if amount <= 0 or rate <= 0 or not math.isfinite(amount) or not math.isfinite(rate):
        raise ScoreError("价格记录无效")
    return amount * rate / (12 if source.get("period") == "y" else 1)


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text())
        if record["fingerprint"] != machine_fingerprint():
            return None
        source = record.get("input")
        monthly = monthly_from_source(source) if isinstance(source, dict) and "amount" in source else float(record["monthly_price_usd"])
        if monthly <= 0 or not math.isfinite(monthly):
            return None
        record["monthly_price_usd"] = monthly
        return record
    except (OSError, ValueError, KeyError, TypeError, ScoreError):
        pass
    return None


def _write_record(path: Path, monthly: float, source: dict[str, Any]) -> None:
    record = {"fingerprint": machine_fingerprint(), "monthly_price_usd": monthly, "input": source}
    path.write_text(json.dumps(record, indent=2) + "\n")


def format_decimal(value: Decimal) -> str:
    """固定小数。先收到 10 位再去掉尾零，避免有效数字格式和无限循环小数。"""
    quantized = value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def amount_text(source: Mapping[str, Any]) -> str:
    text = source.get("text")
    if isinstance(text, str) and text:
        return text
    return format_decimal(Decimal(str(source["amount"])))


def monthly_text(source: Mapping[str, Any], monthly: float) -> str:
    text = source.get("text")
    if isinstance(text, str) and text:
        divisor = Decimal(12) if source.get("period") == "y" else Decimal(1)
        rate = Decimal(str(source.get("rate", 1.0)))
        return format_decimal(Decimal(text) * rate / divisor)
    return format_decimal(Decimal(str(monthly)))


def describe_price(record: Mapping[str, Any]) -> str:
    """给人看的当前价格。有原始输入时带上币种和月/年，并给出折合的美元/月。"""
    monthly = float(record["monthly_price_usd"])
    source = record.get("input")
    if not isinstance(source, dict) or "amount" not in source:
        return f"{format_decimal(Decimal(str(monthly)))} 美元/月"
    period = "年" if source.get("period") == "y" else "月"
    currency = str(source.get("currency") or "USD")
    text = f"{amount_text(source)} {currency}/{period}"
    if currency != "USD" or source.get("period") == "y":
        text += f"（折合 {monthly_text(source, monthly)} 美元/月）"
    return text


def _confirm_reuse(record: Mapping[str, Any], ask: Callable[[str], str]) -> bool:
    """展示当前价格。回车沿用，y 重新输入。返回是否沿用。"""
    prompt = f"当前价格：{describe_price(record)}。重新输入？[y/N]: "
    while True:
        try:
            answer = ask(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return True
        if answer in ("", "n", "no"):
            return True
        if answer in ("y", "yes"):
            return False
        print("请输入 y 或直接回车。")


def _priced(
    raw: str, currency: str, period: str, fetch_rate: Callable[[str], tuple[float, str]]
) -> tuple[float, dict[str, Any]]:
    text = raw.strip().replace(",", "")
    monthly, source = to_monthly_usd(parse_amount(text), currency, period, fetch_rate)
    source["text"] = text
    return monthly, source


def _accept_parsed(monthly: float, source: Mapping[str, Any], ask: Callable[[str], str]) -> bool:
    """复述刚解析的金额。回车确认后才允许保存并开始测量。"""
    shown = describe_price({"monthly_price_usd": monthly, "input": source})
    prompt = f"将保存 {shown}。使用这个价格？[Y/n]: "
    while True:
        answer = ask(prompt).strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def _ask_usd_amount(
    ask: Callable[[str], str], period: str, fetch_rate: Callable[[str], tuple[float, str]]
) -> tuple[float, dict[str, Any]] | None:
    label = "年" if period == "y" else "月"
    while True:
        raw = ask(f"{label}价金额（USD）: ").strip()
        if not raw:
            return None
        try:
            return _priced(raw, "USD", period, fetch_rate)
        except ScoreError as error:
            print(error)


def _ask_price(
    ask: Callable[[str], str], fetch_rate: Callable[[str], tuple[float, str]], *, replacing: bool = False
) -> tuple[float, dict[str, Any]] | None:
    if replacing:
        print("请输入新价格（金额直接回车则仍用当前价格）。")
    else:
        print("这台机器还没有价格记录，用于计算性价比（直接回车可跳过）。")
    while True:
        while True:
            period = ask("计价方式 [m=月价 / y=年价]（默认 m）: ").strip().lower() or "m"
            if period in ("m", "y"):
                break
            print("请输入 m 或 y。")
        while True:
            try:
                currency = normalize_currency(ask("币种 USD / EUR / CNY(RMB) 等（默认 USD）: ") or "USD")
                break
            except ScoreError as error:
                print(error)
        label = "年" if period == "y" else "月"
        while True:
            raw = ask(f"{label}价金额（{currency}）: ").strip()
            if not raw:
                return None
            text = raw.strip().replace(",", "")
            try:
                amount = parse_amount(text)
            except ScoreError as error:
                print(error)
                continue
            try:
                monthly, source = to_monthly_usd(amount, currency, period, fetch_rate)
            except ScoreError as error:
                print(error)
                print("改为直接输入美元金额。")
                offer = _ask_usd_amount(ask, period, fetch_rate)
                if offer is None:
                    return None
                break
            source["text"] = text
            offer = monthly, source
            break
        monthly, source = offer
        if _accept_parsed(monthly, source, ask):
            return monthly, source
        print("已取消这次输入，请重新填写。")


def resolve_monthly_price(
    env: Mapping[str, str] = os.environ,
    record_path: Path = LOCAL_RECORD,
    *,
    ask: Callable[[str], str] | None = None,
    fetch_rate: Callable[[str], tuple[float, str]] = fetch_usd_rate,
) -> tuple[float | None, str]:
    """返回 (美元/月 或 None, 备注)。ask 为 None 表示非交互，不会询问。永不抛异常。"""
    monthly_raw, yearly_raw = env.get("BENCHMARK_MONTHLY_PRICE"), env.get("BENCHMARK_YEARLY_PRICE")
    if monthly_raw and yearly_raw:
        return None, "(BENCHMARK_MONTHLY_PRICE 与 BENCHMARK_YEARLY_PRICE 只能设置一个)"
    if monthly_raw or yearly_raw:
        try:
            currency = normalize_currency(env.get("BENCHMARK_PRICE_CURRENCY") or "USD")
            amount = parse_amount(monthly_raw or yearly_raw)
            return to_monthly_usd(amount, currency, "m" if monthly_raw else "y", fetch_rate)[0], ""
        except ScoreError as error:
            return None, f"({error})"

    record = _read_record(record_path)
    replacing = False
    if record and ask is not None:
        if _confirm_reuse(record, ask):
            return float(record["monthly_price_usd"]), ""
        replacing = True
    elif record:
        return float(record["monthly_price_usd"]), ""
    elif ask is None:
        return None, "(本机无价格记录；设置 BENCHMARK_MONTHLY_PRICE 或 BENCHMARK_YEARLY_PRICE 后重跑)"
    try:
        answer = _ask_price(ask, fetch_rate, replacing=replacing)
    except (EOFError, KeyboardInterrupt):
        print()
        if record:
            return float(record["monthly_price_usd"]), ""
        return None, "(已跳过价格输入)"
    if answer is None:
        if record:
            return float(record["monthly_price_usd"]), ""
        return None, "(已跳过价格输入)"
    monthly, source = answer
    try:
        _write_record(record_path, monthly, source)
    except OSError as error:
        print(f"无法保存价格记录（{error}），本次仍会使用。")
    return monthly, ""


def interactive_ask() -> Callable[[str], str] | None:
    return input if sys.stdin.isatty() and sys.stdout.isatty() else None
