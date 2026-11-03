# Calculator

## What It Does
Evaluates mathematical expressions, performs unit conversions, and computes financial values. Handles arithmetic, trigonometry, percentages, compound interest, and mortgage calculations.

## When to Use It
- User says "calculate", "convert", "how much is", "what's X% of Y"
- User needs unit conversion (miles↔km, kg↔lbs, etc.)
- User wants financial math (interest, tips, tax, mortgage)
- Numbers and operators are present in the message

## When NOT to Use It
- User wants to *write* a calculator program → use code_explainer
- Question is conceptual, not computational
- Currency conversion → use currency skill instead

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `evaluate` | any math expression. |
| `percentage` | what % part is of whole. |
| `percentage_change` | % change from original to new. |
| `compound_interest` | compound interest calculation. |

## Output Format
```json
{
  "expression": "2 + 2 * 3",
  "result": 8,
  "unit_from": null,
  "unit_to": null,
  "steps": "2 + (2 * 3) = 2 + 6 = 8"
}
```

## Constraints
- Expressions evaluated as safe Python math (whitelisted functions only)
- Supported: sin, cos, tan, sqrt, log, floor, ceil, abs, round
- No arbitrary code execution
- Financial results are estimates — not financial advice

## Examples
- "15% tip on a ₦4,500 bill"
- "Convert 200 pounds to kilograms"
- "What's sin(45 degrees)?"
- "How much interest on ₦50,000 at 8% for 6 months?"
