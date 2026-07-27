@echo off
setlocal
cd /d "%~dp0"

rem One-time presentation profile. These values apply only to this process.
set "EXPERTSEARCH_DEMO_MODE=true"
set "EXPERTSEARCH_DEMO_TOTAL_EXPERTS=10"
set "SUBDOMAIN_TARGET_EXPERTS=10"
set "RESEARCHER_TARGET_EXPERTS=10"
set "RESEARCHER_CHUNK_SIZE=10"
set "SUBDOMAIN_TIER_RECOVERY_ATTEMPTS=1"
set "SUBDOMAIN_FINAL_TOPUP_ATTEMPTS=2"
set "FINAL_ENRICHMENT_MAX_S2_ROWS=5"
set "EXPERT_ENRICHMENT_MAX_HOMEPAGE_ROWS=10"
set "EXPERT_ENRICHMENT_MAX_TAVILY_ROWS=6"
set "SURVIVAL_VERIFICATION_MAX_ROWS=10"
set "OPENCLI_HOMEPAGE_MAX_CALLS_PER_PROCESS=10"

echo Starting ExpertSearch one-time demo profile...
echo Demo output target: 10 experts from one sub-domain.
echo The normal .env file and main branch are not modified.
echo.

call "%~dp0start_streamlit_logged.cmd"

endlocal
