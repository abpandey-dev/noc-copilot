# Runbook: Degradation after a configuration change

**Symptoms:** A KPI (often drop-call rate or handover success) degrades on the cells of one site minutes after a parameter change on that site. Alarms such as HANDOVER_FAILURE_RATE_HIGH may or may not fire.

**Diagnosis:**
1. Match the start of the KPI degradation to the change-log timestamp on the same entity.
2. Ignore changes on other entities with no KPI impact.

**Actions (need NOC approval):**
- Roll back the parameter change to the last known-good value.
- Pause the automation (SON) policy that pushed it until reviewed.
- Monitor drop-call rate for 30 minutes after rollback.
