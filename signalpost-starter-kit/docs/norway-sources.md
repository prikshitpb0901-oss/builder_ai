# Norwegian source map

## Brønnøysundregistrene

| Data | Endpoint | POC use |
|---|---|---|
| Full entity snapshot | `https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` | Daily NLOD 2.0 identity universe and frozen sampling |
| Live entity | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}` | Identity, form, status, industry, address, website, employees, latest account flag |
| Public roles | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller` | Company-centric public role holders; dates of birth are discarded |
| Group structure | `https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}` | Official corporate relationships when returned |
| Subunits | `https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}` | Registered establishments, addresses, industries, and reported employees |
| Latest normalized accounts | `https://data.brreg.no/regnskapsregisteret/regnskap/{org}` | Latest normalized annual-account fields when covered |
| Available filing years | `https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar` | Historical year discovery, rate limited to roughly 30 requests/minute |
| Annual-account PDF | `https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}` | Official filing copy for a discovered year |

The bulk snapshot and live API are compared by exact organisation number. A 410 response means any
cached copy must be removed. Person data is only used in the company-centric role context; birth dates
are not stored or displayed.

## Coverage limits

- Public annual accounts do not exist for every registered entity. AS and ASA generally file; many sole
  proprietorships do not. An endpoint 404 is evidence of no returned record, not evidence of zero.
- The normalized account endpoint returned source errors for a small bank/insurer slice in the POC.
  Those cases remain explicit source errors and can be routed to official PDFs or regulated-sector data.
- The annual shareholder register is available from the Norwegian Tax Administration by request and is
  not a same-day dependency. It may take several business days and requires GDPR-aware handling.
- The beneficial-owner register is not a general public enrichment source for this POC.
- Company websites are a company-reported layer, not official proof. Search/news/social sources require a
  lawful, declared connector and their own evidence classification.
