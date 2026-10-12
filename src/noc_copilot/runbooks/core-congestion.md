# Runbook: Packet core (UPF) congestion

**Symptoms:** User-plane latency and packet loss rise on the core; UPF CPU very high; throughput drops on cells in every region at once while cells stay in service. UPF_CPU_HIGH may not fire if the threshold is mis-set; GTPU_PATH_DEGRADED is a secondary symptom.

**Diagnosis:**
1. Degradation is network-wide, not tied to one region or router.
2. Core latency / loss / CPU anomalies line up with the cell throughput drop.

**Actions (need NOC approval):**
- Scale out UPF instances or shift traffic to the standby UPF.
- Check for a traffic spike or a recent core software change.
- Inform the core operations team; open a major incident.
