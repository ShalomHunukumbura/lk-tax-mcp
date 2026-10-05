# lk-tax-mcp

**Exact, sourced Sri Lankan tax calculations for AI assistants.** An [MCP](https://modelcontextprotocol.io)
server that lets Claude (or any MCP client) calculate APIT, income tax, withholding tax, VAT and SSCL from
the official Inland Revenue Department rules for any date since 1 January 2023, instead of recalling
rates from memory.

<!-- mcp-name: io.github.ShalomHunukumbura/lk-tax-mcp -->

## What it looks like

A real Claude Code session with only this server connected:

> **You:** I'm paid Rs. 350,000 a month in Sri Lanka. How much APIT will my employer deduct this month,
> and how much was it before the April 2025 change?

Claude calls `calculate_salary_tax` twice, once for today and once for 31 March 2025, and answers (abridged):

> **This month: Rs. 32,500 APIT.** The first Rs. 150,000/month is tax-free, then 6% / 18% / 24% / 30%.
> Source: IRD Notice PN/IT/2025-01 and APIT Tax Table No. 01 (2025/26). In force since 1 April 2025.
>
> **Before the change (to 31 March 2025): Rs. 52,500.** Rs. 100,000/month tax-free, then
> 6% / 12% / 18% / 24% / 30% / 36%. Source: APIT Tax Table No. 01 (2023/24 & 2024/25).
>
> So you now pay **Rs. 20,000 less per month**. This assumes it's your regular pay from your main job and
> you're a Sri Lankan tax resident; one-off bonuses or a second job use different tables.

The numbers come from the tool, the caveats come from the server's instructions, and every figure is
traceable to an IRD document.

## Why

Language models are confident about tax rates, and Sri Lanka's change often: in 2025 the salary tax bands
were rebuilt, and in 2026 alone the SSCL threshold dropped, VAT on financial services rose to 20.5% and
withholding tax was extended to more professions, all mid-year. A model recalling "the rate" is wrong for
some dates and has no source to show. This server gives the model exact arithmetic over dated, sourced
rules, built on [lk-tax](https://github.com/ShalomHunukumbura/lk-tax), which is tested against every row
of the IRD's official APIT tables.

## Install

Requires [uv](https://docs.astral.sh/uv/) (`uvx` runs the server without a manual install).

**Claude Code**

```bash
claude mcp add lk-tax -- uvx lk-tax-mcp
```

**Claude Desktop**: add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "lk-tax": { "command": "uvx", "args": ["lk-tax-mcp"] }
  }
}
```

**Any other MCP client**: run `uvx lk-tax-mcp` as a stdio server, or `pip install lk-tax-mcp` and run `lk-tax-mcp`.

## Tools

| Tool | Answers |
|---|---|
| `calculate_salary_tax` | APIT to deduct from a month's salary (official Tax Table No. 01 amount) |
| `calculate_annual_income_tax` | Income tax for a year of assessment, with band breakdown |
| `calculate_withholding_tax` | WHT on 11 payment types: thresholds, exemptions, grossing up |
| `calculate_vat` | Add VAT to a price or extract it from a VAT-inclusive price |
| `check_vat_registration` | Does a business need to register for VAT? |
| `calculate_sscl` | SSCL on turnover by activity (liable share × 2.5%) |
| `check_sscl_registration` | Does a business need to register for SSCL? |
| `get_tax_rules` | Rates, bands and thresholds in force on a date |
| `get_rule_history` | Every change to a tax since 2023, with dates and sources |

Every result includes a one-line `summary`, exact amounts as decimal strings, and the rule version used
(`effective_from`, `effective_to`, IRD `sources`).

## Design notes

- **Read-only and offline.** Every tool is a pure calculation, annotated `readOnlyHint` and
  `idempotentHint`. No network access, no files, no credentials.
- **Dates are explicit.** Each calculation takes the date it applies to (pay date, invoice date), so
  "what was it last year?" gets last year's rules.
- **Errors the model can act on.** Bad input comes back as a tool error explaining what to fix
  (`monthly_income must be a number`, `lk-tax covers 2023-01-01 onwards`), not a generic failure.
- **Choices are enumerated.** Payment types and SSCL activities are enums in the schema, each described,
  so the model picks a valid one.
- **Honest about gaps.** The server instructions tell the model what isn't covered (lump sums,
  non-resident employees, VAT exemption classification, dates before 2023) and to say so rather than
  estimate.

## Development

```bash
uv sync
uv run pytest                    # includes a test that launches the real command over stdio
uv run mcp dev src/lk_tax_mcp/server.py   # explore the tools in the MCP Inspector
```

Built with the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2.

## Disclaimer

Not tax advice. Results link to the IRD sources they come from, so you can check them.

## License

MIT
