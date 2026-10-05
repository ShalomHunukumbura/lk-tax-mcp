import asyncio
import shutil
import sys
import typing
from decimal import Decimal

import pytest
from lk_tax import income_tax, sscl, vat, wht
from mcp import Client, StdioServerParameters

from lk_tax_mcp import server
from lk_tax_mcp.server import mcp


def run(coro):
    return asyncio.run(coro)


async def _call(name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


def call(name, **args):
    result = run(_call(name, args))
    assert not result.is_error, result.content[0].text
    return result.structured_content


def call_error(name, **args):
    result = run(_call(name, args))
    assert result.is_error
    return result.content[0].text


async def _tools():
    async with Client(mcp) as client:
        return (await client.list_tools()).tools


TOOLS = run(_tools())


# --- the tool surface the model sees ------------------------------------------


def test_tool_list():
    assert [t.name for t in TOOLS] == [
        "calculate_salary_tax", "calculate_annual_income_tax", "calculate_withholding_tax", "calculate_vat",
        "check_vat_registration", "calculate_sscl", "check_sscl_registration", "get_tax_rules", "get_rule_history",
    ]


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_tools_are_marked_read_only_and_documented(tool):
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.description and len(tool.description) > 40
    for name, prop in tool.input_schema["properties"].items():
        assert prop.get("description"), f"{tool.name}.{name} has no description"


def test_choice_lists_match_the_library():
    assert set(typing.get_args(server.PaymentType)) == set(wht.payment_types())
    assert set(typing.get_args(server.Activity)) == set(sscl.activities())


def test_enum_appears_in_schema():
    tool = next(t for t in TOOLS if t.name == "calculate_withholding_tax")
    assert "interest" in tool.input_schema["properties"]["payment_type"]["enum"]


# --- results match lk-tax and cite their source -------------------------------


def test_salary_tax():
    r = call("calculate_salary_tax", monthly_income=350_000, pay_date="2026-09-30")
    assert r["tax"] == "32500.00" == str(income_tax.apit_monthly(350_000, "2026-09-30").tax)
    assert r["rule"]["effective_from"] == "2025-04-01"
    assert r["rule"]["sources"][0]["url"].startswith("https://www.ird.gov.lk/")
    assert "Rs. 32,500.00" in r["summary"] and "PN/IT/2025-01" in r["summary"]


def test_salary_tax_accepts_strings_and_uses_old_rules_for_old_dates():
    assert call("calculate_salary_tax", monthly_income="200000", pay_date="2025-03-31")["tax"] == "10500.00"


def test_annual_income_tax():
    r = call("calculate_annual_income_tax", annual_income=4_000_000, year_of_assessment="2025/26")
    assert r["tax"] == "330000.00" and "8.25%" in r["summary"]


def test_withholding_tax():
    r = call("calculate_withholding_tax", payment_type="interest", amount=100_000, payment_date="2025-04-01")
    assert r["tax"] == "10000.00" and r["net_paid"] == "90000.00"
    below = call("calculate_withholding_tax", payment_type="service_fee", amount=80_000, payment_date="2025-06-01")
    assert below["applies"] is False and below["summary"].startswith("No WHT")
    grossed = call("calculate_withholding_tax", payment_type="service_fee", amount=190_000,
                   payment_date="2025-06-01", payer_bears_tax=True)
    assert grossed["gross"] == "200000.00"


def test_vat():
    assert call("calculate_vat", amount=118, mode="extract", invoice_date="2025-01-01")["vat"] == "18.00"
    assert call("calculate_vat", amount=100, supply="financial_services", invoice_date="2026-07-01")["vat"] == "20.50"


def test_registration_checks():
    assert call("check_vat_registration", turnover_12_months=70_000_000, on_date="2024-01-01")["must_register"] is True
    r = call("check_sscl_registration", quarter_turnover=12_000_000, on_date="2026-07-01")
    assert r["must_register"] is True and r["summary"].startswith("Must register for SSCL")
    assert call("check_sscl_registration", quarter_turnover=12_000_000, on_date="2026-06-30")["must_register"] is False


def test_sscl():
    r = call("calculate_sscl", turnover=1_000_000, activity="manufacture", on_date="2025-01-01")
    assert r["levy"] == str(sscl.levy(1_000_000, "manufacture", "2025-01-01").levy) == "21250.00"


def test_get_tax_rules():
    r = call("get_tax_rules", tax="vat", on_date="2023-06-01")
    assert r["values"]["standard_rate"] == "0.15" and r["effective_to"] == "2023-12-31"
    w = call("get_tax_rules", tax="wht", on_date="2025-04-01")
    assert w["payment_types"]["interest"]["values"]["rate"] == "0.10"
    s = call("get_tax_rules", tax="sscl", on_date="2026-09-30")
    assert s["values"]["annual_threshold"] == 36_000_000 and "manufacture" in s["activities"]


def test_get_rule_history():
    r = call("get_rule_history", tax="vat")
    assert [v["effective_from"] for v in r["versions"]] == ["2023-01-01", "2024-01-01", "2026-07-01"]
    assert r["versions"][-1]["effective_to"] is None
    w = call("get_rule_history", tax="wht")
    assert [v["values"]["rate"] for v in w["payment_types"]["interest"]["versions"]] == ["0.05", "0.10"]


# --- errors reach the model with a useful message -------------------------------


@pytest.mark.parametrize("tool,args,expected", [
    ("calculate_salary_tax", {"monthly_income": "lots"}, "monthly_income must be a number"),
    ("calculate_salary_tax", {"monthly_income": -5}, "cannot be negative"),
    ("calculate_salary_tax", {"monthly_income": 1, "pay_date": "2020-01-01"}, "Not covered"),
    ("calculate_salary_tax", {"monthly_income": 1, "pay_date": "30/09/2026"}, "dates must look like"),
    ("calculate_annual_income_tax", {"annual_income": 1, "year_of_assessment": "2022/23"}, "Not covered"),
    ("check_vat_registration", {}, "give period_turnover"),
    ("calculate_withholding_tax", {"payment_type": "salary", "amount": 1}, "payment_type"),
])
def test_errors_are_explained(tool, args, expected):
    message = call_error(tool, **args)
    assert expected in message
    assert message != f"Error executing tool {tool}"


# --- the real thing: launch the installed command over stdio --------------------


def test_console_script_over_stdio():
    command = shutil.which("lk-tax-mcp")
    params = (StdioServerParameters(command=command) if command
              else StdioServerParameters(command=sys.executable, args=["-m", "lk_tax_mcp"]))

    async def session():
        async with Client(params) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("calculate_salary_tax", {"monthly_income": 350000, "pay_date": "2026-09-30"})
            return names, result.structured_content["tax"]

    names, tax = run(session())
    assert "calculate_salary_tax" in names and Decimal(tax) == Decimal("32500")
