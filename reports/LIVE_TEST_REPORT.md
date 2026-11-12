# Live storefront verification

Run on 11 November 2026 against the 18 original failed storefront seeds. The test budget was one discovery page, up to two detail attempts and one saved product per site. The live tests requested fresh robots policies. Gap was retested after fixing canonical PID matching.

## Outcomes

| Store | Robots | Platform | Discovered / extracted | Method | Missing fields | Failed request or stage | Final status |
|---|---|---|---:|---|---|---|---|
| Revolve | robots_timeout | unknown | 0 / 0 | — | — | robots ROBOTS_TIMEOUT | Technical or policy verification failure |
| ASOS | robots_timeout | unknown | 0 / 0 | — | — | robots ROBOTS_TIMEOUT | Technical or policy verification failure |
| Zara | http_403 | unknown | 0 / 0 | — | — | robots HTTP_403 (403) | Access denied |
| McQueen | tls_error | unknown | 0 / 0 | — | — | robots TLS_ERROR | Technical or policy verification failure |
| Versace | tls_error | unknown | 0 / 0 | — | — | robots TLS_ERROR | Technical or policy verification failure |
| H&M | http_403 | unknown | 0 / 0 | — | — | robots HTTP_403 (403) | Access denied |
| Mango | http_429 | unknown | 0 / 0 | — | — | robots HTTP_429 (429) | Rate limited |
| UNIQLO | robots_timeout | unknown | 0 / 0 | — | — | robots ROBOTS_TIMEOUT | Technical or policy verification failure |
| adidas | http_403 | unknown | 0 / 0 | — | — | robots HTTP_403 (403) | Access denied |
| New Balance | tls_error | unknown | 0 / 0 | — | — | robots TLS_ERROR | Technical or policy verification failure |
| Levi's | fetched | unknown | 0 / 0 | — | — | listing HTTP_403 (403) | Access denied |
| Gap | fetched | custom | 5 / 1 | http:structured_json | None | — | Product detail |
| Abercrombie | fetched | custom | 0 / 0 | — | — | browser BROWSER_HTTP_ERROR (418) | Browser HTTP failure |
| Aritzia | http_403 | unknown | 0 / 0 | — | — | robots HTTP_403 (403) | Access denied |
| Gucci | robots_timeout | unknown | 0 / 0 | — | — | robots ROBOTS_TIMEOUT | Technical or policy verification failure |
| Prada | robots_timeout | unknown | 0 / 0 | — | — | robots ROBOTS_TIMEOUT | Technical or policy verification failure |
| Coach | redirect_out_of_scope | unknown | 0 / 0 | — | — | robots REDIRECT_OUT_OF_SCOPE | Technical or policy verification failure |
| Michael Kors | fetched | unknown | 0 / 0 | — | — | listing HTTP_403 (403) | Access denied |

Gap returned Dream Eau de Parfum, USD 52, from a canonical product page discovered by the tested listing seed. The run verified one product detail. Abercrombie returned HTTP 418 from the isolated browser run. The prior memory crash did not recur in that run.

See [structured job and request diagnostics](upgrade-live-results.jsonl) for URLs, status codes, retry hints, redirect chains, and samples.

## Authorized alternatives

Request an approved feed, partner export or documented API from the operator if storefront access is restricted or remains unavailable. Use a fresh authorized regional seed for Coach after confirming its hostname. Revolve advertises an affiliate product catalog, Levi's offers an affiliate program, and Michael Kors advertises a live product feed for its EU affiliate program. Eligibility and access were not verified.

- [Revolve affiliate program](https://www.revolve.com/affiliate)
- [Levi's affiliate program](https://www.levi.com/US/en_US/features/affiliate-program)
- [Michael Kors EU affiliate program](https://ui.awin.com/merchant-profile/15215)
