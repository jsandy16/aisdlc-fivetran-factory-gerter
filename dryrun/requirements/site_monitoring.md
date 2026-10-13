# Requirement: Site monitoring visit reports (CRO FTPS feed)

**Business owner:** Clinical Operations — Site Performance
**Purpose:** Give clinical operations a daily view of monitoring visits and open issues per investigator site,
so that overdue sites are escalated within one business day.

## Source

The CRO publishes one CSV file per day on its FTPS server `ftps.cro-partner.example` (port 21, explicit TLS,
passive mode). Our account is `az_sitemon`. Files are under `/exports/monitoring` and are named
`SMV_YYYYMMDD_NN.csv`, where NN is a two-digit sequence number for re-issues on the same day. Files must be
loaded in date then sequence order. Files are complete once they are at least 10 minutes old.

## Data

Each row is one monitoring visit. Columns:

| Column      | Meaning                                       | Notes                                  |
|-------------|-----------------------------------------------|----------------------------------------|
| VISIT_ID    | CRO identifier of the monitoring visit        | unique, mandatory                      |
| SITE_NO     | Investigator site number                      | mandatory, 4 digits                    |
| STUDY       | Study code, e.g. D1234C00001                  | mandatory                              |
| VISIT_TYPE  | SIV, IMV, COV (initiation, interim, close-out)| mandatory                              |
| VISIT_DATE  | Date of the visit, format YYYY-MM-DD          | mandatory                              |
| REPORT_DATE | Date the report was finalised, YYYY-MM-DD     | optional until finalised               |
| OPEN_ISSUES | Number of open issues at the site             | whole number, zero or more             |
| CRA_EMAIL   | Email of the monitor                          | personal data                          |

Rules from the business:
- A report must be finalised no more than 10 calendar days after the visit date.
- Open issues can never be negative.
- Close-out visits (COV) must have zero open issues.

The feed should refresh every 6 hours. Data is needed for 7 years.
