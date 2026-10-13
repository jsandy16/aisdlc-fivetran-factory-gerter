# testing/ (Phases 2–5)

Test stand-ins:
- a mock portal (httpx MockTransport + a TLS/mTLS server);
- a Vault dev server harness;
- SFTP/FTPS/HTTPS servers (extended from `dryrun/servers.py`);
- moto;
- the Airflow DAG-integrity harness (MWAA local runner).
