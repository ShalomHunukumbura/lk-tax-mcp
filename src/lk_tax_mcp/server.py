"""MCP server exposing lk-tax: exact, sourced Sri Lankan tax calculations for AI assistants.

Every tool is a pure calculation over lk-tax's effective-dated rules, so all of
them are read-only and idempotent. Results are the lk-tax result objects as
JSON, plus a one-line `summary`, and always carry the rule version used and its
Inland Revenue Department (IRD) sources.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any, Literal, TypeVar

import lk_tax
from lk_tax import NotCovered, income_tax, sscl, to_dict, vat, wht
from lk_tax._core import as_date, load_rules, select_version
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

INSTRUCTIONS = """\
Exact Sri Lankan tax calculations from the official Inland Revenue Department (IRD) rules, \
for any date from 1 January 2023: APIT on salaries, annual personal income tax, withholding tax, \
VAT and SSCL.

Use these tools instead of recalling rates from memory: Sri Lankan rates and thresholds changed \
several times in 2025 and 2026, often mid-year. Pass the date the question is about (for example \
the pay date or invoice date); it defaults to today.

Amounts are Sri Lankan rupees (LKR), returned as exact decimal strings. Every result includes the \
rule version that applied and links to the IRD source. When you give a figure, mention which rule \
it came from. If the user's situation isn't covered (for example lump-sum or once-off employment \
payments, non-resident employees, deciding whether a supply is VAT-exempt, or dates before 2023), \
say so rather than estimating. These are calculations, not tax advice.
"""

mcp = MCPServer("lk-tax", instructions=INSTRUCTIONS, version=lk_tax.__version__)

PURE = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

Money = Annotated[int | float | str, Field(description="Amount in Sri Lankan rupees, e.g. 350000 or \"350000.50\".")]
OnDate = Annotated[
    str | None,
    Field(description="Date the rules apply to, as YYYY-MM-DD (e.g. the pay date or invoice date). Defaults to today."),
]
PaymentType = Literal[
    "service_fee",
    "interest",
    "rent_resident",
    "dividend",
    "winnings",
    "royalty",
    "charge_or_premium",
    "rent_non_resident",
    "service_fee_non_resident",
    "transport_non_resident",
    "gem_auction",
]
Activity = Literal["import", "manufacture", "service", "distributor", "wholesale_retail"]
TaxName = Literal["income_tax", "wht", "vat", "sscl"]

F = TypeVar("F", bound=Callable[..., Any])


def _user_errors(fn: F) -> F:
    """Turn lk-tax input errors into ToolError, the exception whose message the model sees.

    Any other exception is a bug, and the SDK deliberately hides its text from the model.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except NotCovered as exc:
            raise ToolError(f"Not covered: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def _rs(value: Any) -> str:
    return f"Rs. {float(value):,.2f}"


def _rule_line(rule: dict[str, Any]) -> str:
    until = f"until {rule['effective_to']}" if rule["effective_to"] else "and still in force"
    return f"Rule in force from {rule['effective_from']} {until}; source: {rule['sources'][0]['title']}."


@mcp.tool(annotations=PURE)
@_user_errors
def calculate_salary_tax(monthly_income: Money, pay_date: OnDate = None) -> dict[str, Any]:
    """APIT (Advance Personal Income Tax) an employer deducts from one month's salary.

    Uses APIT Tax Table No. 01: regular monthly employment income (salary plus allowances and
    taxable benefits) of a resident employee, or a non-resident Sri Lankan citizen, in their
    primary employment. `tax` is the amount from the official table, which is what payroll
    deducts; `exact_tax` is the underlying formula to the cent. Not for lump sums, bonuses paid
    once, secondary employment or non-resident foreign employees.
    """
    result = to_dict(income_tax.apit_monthly(monthly_income, pay_date))
    result["summary"] = (
        f"APIT on a monthly income of {_rs(result['monthly_income'])} paid on {result['on']} is "
        f"{_rs(result['tax'])}. The first {_rs(result['monthly_relief'])} a month is tax-free. "
        + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def calculate_annual_income_tax(
    annual_income: Annotated[
        int | float | str,
        Field(description="Total assessable income for the year of assessment, before personal relief, in rupees."),
    ],
    year_of_assessment: Annotated[
        str, Field(description="Year of assessment (April to March), e.g. \"2025/26\". Supported from 2023/24.")
    ],
) -> dict[str, Any]:
    """Personal income tax for a resident individual for a whole year of assessment.

    Applies the personal relief and progressive rates for that year and returns the tax,
    the band-by-band breakdown and the effective rate.
    """
    result = to_dict(income_tax.annual(annual_income, year_of_assessment))
    result["summary"] = (
        f"Income tax for Y/A {result['year_of_assessment']} on {_rs(result['income'])} is {_rs(result['tax'])} "
        f"(personal relief {_rs(result['personal_relief'])}, effective rate "
        f"{float(result['effective_rate']) * 100:.2f}%). " + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def calculate_withholding_tax(
    payment_type: Annotated[
        PaymentType,
        Field(
            description=(
                "service_fee: fees to a resident individual who isn't an employee (teaching, commissions, "
                "independent professionals). interest: interest or discount. rent_resident: rent to a resident. "
                "dividend. winnings: lottery, betting or gambling. royalty. charge_or_premium: charges, natural "
                "resource payments, premiums. rent_non_resident. service_fee_non_resident: service fees or "
                "insurance premiums to a non-resident. transport_non_resident: transport or telecom services "
                "by a non-resident. gem_auction: gems sold at a National Gem & Jewellery Authority auction."
            )
        ),
    ],
    amount: Annotated[
        int | float | str,
        Field(description="The payment in rupees, excluding VAT. With payer_bears_tax, the amount the payee must receive."),
    ],
    payment_date: OnDate = None,
    month_total: Annotated[
        int | float | str | None,
        Field(
            description=(
                "For service fees and rent: total paid to this person in the calendar month, including this "
                "payment. WHT only applies once this exceeds Rs. 100,000. Defaults to the amount."
            )
        ),
    ] = None,
    payer_bears_tax: Annotated[
        bool, Field(description="True if the payer pays the WHT on top so the payee receives the amount in full.")
    ] = False,
) -> dict[str, Any]:
    """Withholding tax (WHT / AIT) the payer must deduct from a payment and pay to the IRD.

    Handles the Rs. 100,000 monthly thresholds for service fees and rent (once exceeded, WHT applies
    to the full payment), exempt small lottery winnings, and grossing up when the payer bears the tax.
    """
    result = to_dict(
        wht.calculate(payment_type, amount, payment_date, month_total=month_total, payer_bears_tax=payer_bears_tax)
    )
    if result["applies"]:
        head = (
            f"WHT on a {result['payment_name'].lower()} of {_rs(result['gross'])} is {_rs(result['tax'])} "
            f"({float(result['rate']) * 100:g}%); the payee receives {_rs(result['net_paid'])}."
        )
    else:
        head = f"No WHT: {result['reason']}"
    result["summary"] = f"{head} " + _rule_line(result["rule"])
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def calculate_vat(
    amount: Money,
    mode: Annotated[
        Literal["add", "extract"],
        Field(description="add: the amount excludes VAT. extract: the amount already includes VAT."),
    ] = "add",
    supply: Annotated[
        Literal["standard", "financial_services"],
        Field(description="standard for most supplies; financial_services for VAT on financial services."),
    ] = "standard",
    invoice_date: OnDate = None,
) -> dict[str, Any]:
    """Add VAT to a price, or work out the VAT included in a VAT-inclusive price.

    The caller decides whether the supply is taxable: this tool does not classify goods or
    services as exempt or zero-rated.
    """
    fn = vat.add if mode == "add" else vat.extract
    result = to_dict(fn(amount, invoice_date, supply))
    result["summary"] = (
        f"VAT at {float(result['rate']) * 100:g}% on {result['on']}: net {_rs(result['net'])} + VAT "
        f"{_rs(result['vat'])} = {_rs(result['gross'])}. " + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def check_vat_registration(
    period_turnover: Annotated[
        int | float | str | None,
        Field(description="Taxable supplies in the taxable period (usually a quarter), in rupees."),
    ] = None,
    turnover_12_months: Annotated[
        int | float | str | None, Field(description="Taxable supplies in the 12 months then ending, in rupees.")
    ] = None,
    on_date: OnDate = None,
) -> dict[str, Any]:
    """Whether a business must register for VAT, given its taxable supplies. Give at least one figure.

    Uses the registration thresholds in force on the date (supplies other than financial services).
    """
    result = to_dict(vat.registration(period_turnover=period_turnover, turnover_12_months=turnover_12_months, on=on_date))
    verdict = "Must register for VAT: " + " ".join(result["reasons"]) if result["must_register"] else "VAT registration is not required on these figures."
    result["summary"] = (
        f"{verdict} Thresholds on {result['on']}: {_rs(result['period_threshold'])} per period or "
        f"{_rs(result['annual_threshold'])} per 12 months. " + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def calculate_sscl(
    turnover: Annotated[int | float | str, Field(description="Turnover from this activity for the period, in rupees.")],
    activity: Annotated[
        Activity,
        Field(
            description=(
                "import: importing (100% liable). manufacture: manufacturing (85%). service: providing services "
                "(100%). distributor: sale by a registered distributor of a Sri Lankan manufacturer (25%). "
                "wholesale_retail: any other wholesale or retail sale, including importing and reselling (50%)."
            )
        ),
    ],
    on_date: OnDate = None,
) -> dict[str, Any]:
    """Social Security Contribution Levy (SSCL): 2.5% of the liable share of turnover for an activity.

    For a business with several activities, call once per activity and add the results.
    Does not cover financial services, which are levied on value addition.
    """
    result = to_dict(sscl.levy(turnover, activity, on_date))
    result["summary"] = (
        f"SSCL on {_rs(result['turnover'])} of {result['activity_name'].lower()} turnover is {_rs(result['levy'])}: "
        f"{float(result['liable_share']) * 100:g}% of turnover is liable, at {float(result['rate']) * 100:g}%. "
        + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def check_sscl_registration(
    quarter_turnover: Annotated[
        int | float | str | None, Field(description="Aggregate turnover for the quarter, in rupees.")
    ] = None,
    turnover_4_quarters: Annotated[
        int | float | str | None, Field(description="Aggregate turnover for four consecutive quarters, in rupees.")
    ] = None,
    on_date: OnDate = None,
) -> dict[str, Any]:
    """Whether a business must register for SSCL, given its aggregate turnover. Give at least one figure."""
    result = to_dict(
        sscl.registration(quarter_turnover=quarter_turnover, turnover_4_quarters=turnover_4_quarters, on=on_date)
    )
    verdict = "Must register for SSCL: " + " ".join(result["reasons"]) if result["must_register"] else "SSCL registration is not required on these figures."
    result["summary"] = (
        f"{verdict} Thresholds on {result['on']}: {_rs(result['quarter_threshold'])} a quarter or "
        f"{_rs(result['annual_threshold'])} over four quarters. " + _rule_line(result["rule"])
    )
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def get_tax_rules(tax: Annotated[TaxName, Field(description="Which tax's rules to return.")], on_date: OnDate = None) -> dict[str, Any]:
    """The rates, bands and thresholds of a tax in force on a date, with their IRD sources.

    Use this to answer "what is the current VAT rate?" or "what were the APIT bands in 2024?".
    For withholding tax, returns every payment type.
    """
    when = as_date(on_date)
    rules = load_rules(tax)
    if tax == "wht":
        shared = rules["_sources"]
        types = {
            key: {"name": spec["name"], **_version(select_version(spec["versions"], when, shared))}
            for key, spec in rules.items()
            if not key.startswith("_")
        }
        return {"tax": tax, "on": when.isoformat(), "payment_types": types}
    result = {"tax": tax, "on": when.isoformat(), **_version(select_version(rules["versions"], when))}
    if tax == "sscl":
        result["activities"] = to_dict(rules["activities"])
    return result


@mcp.tool(annotations=PURE)
@_user_errors
def get_rule_history(tax: Annotated[TaxName, Field(description="Which tax's history to return.")]) -> dict[str, Any]:
    """Every version of a tax's rules since 1 January 2023: what changed, when, and the IRD source.

    Use this for questions like "what changed for SSCL in 2026?" or "when did VAT go to 18%?".
    """
    rules = load_rules(tax)
    if tax == "wht":
        shared = rules["_sources"]
        history = {
            key: {"name": spec["name"], "versions": [_version(r) for r in _all_versions(spec["versions"], shared)]}
            for key, spec in rules.items()
            if not key.startswith("_")
        }
        return {"tax": tax, "payment_types": history}
    return {"tax": tax, "versions": [_version(r) for r in _all_versions(rules["versions"])]}


def _all_versions(versions: list[dict[str, Any]], shared: dict[str, Any] | None = None) -> list[lk_tax.Rule]:
    return [select_version(versions, v["effective_from"], shared) for v in sorted(versions, key=lambda v: v["effective_from"])]


def _version(rule: lk_tax.Rule) -> dict[str, Any]:
    values = {k: v for k, v in rule.data.items() if k not in ("effective_from", "sources", "note")}
    return {
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
        "values": to_dict(values),
        "note": rule.note,
        "sources": to_dict(list(rule.sources)),
    }


def main() -> None:
    """Console entry point: serve over stdio, as MCP hosts expect."""
    mcp.run()


if __name__ == "__main__":
    main()
