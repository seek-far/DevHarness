# BCP 47 Language Tags

## Overview

BCP 47 is the IETF standard used to identify languages and locales on the web.
It appears in places such as HTML `lang` attributes, HTTP `Accept-Language`
headers, JavaScript `Intl`, CSS `:lang()`, CLDR data, and internationalization
libraries.

## Tag Structure

BCP 47 tags are built from subtags separated by hyphens:

```text
language[-script][-region][-variant][-extension]
```

Common subtag types:

| Subtag | Length / Form | Examples | Meaning |
|---|---:|---|---|
| Language | 2-3 letters | `en`, `zh` | ISO 639 language code |
| Script | 4 letters | `Hans`, `Latn` | ISO 15924 writing system |
| Region | 2 letters or 3 digits | `US`, `419` | ISO 3166-1 country/region or UN M.49 region |
| Variant | 5-8 characters | `valencia` | Dialect, spelling, or other language variant |

## Examples

| Tag | Meaning |
|---|---|
| `en` | English without a region-specific variant |
| `en-US` | English as used in the United States |
| `en-GB` | English as used in Great Britain |
| `zh-Hans` | Chinese written with the Simplified script |
| `zh-Hant-TW` | Chinese written with the Traditional script, Taiwan |
| `sr-Latn-RS` | Serbian written with the Latin script, Serbia |
| `pt-BR` | Portuguese as used in Brazil |
| `es-419` | Spanish for Latin America |

## Matching Rules

BCP 47 language matching commonly uses these strategies:

| Rule | Description |
|---|---|
| Basic filtering | Prefix matching, for example `en` can match `en-US`. |
| Extended filtering | Matching with wildcard subtags. |
| Lookup | Finds the best single match by progressively truncating subtags. |

