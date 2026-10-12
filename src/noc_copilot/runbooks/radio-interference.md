# Runbook: Uplink radio interference

**Symptoms:** One cell shows UL interference well above the -110 dBm noise floor, lower throughput and more dropped calls, while its sibling cells are normal. UL_INTERFERENCE_HIGH may be missing if the alarm threshold is disabled.

**Diagnosis:**
1. Compare the cell with the other sectors of the same site: interference on a single sector points to an external source or a faulty antenna/feeder on that sector.
2. Rule out site-level problems (power, transport) since the site stays in service.

**Actions (need NOC approval):**
- Run a spectrum scan on the affected sector.
- Check for a passive intermodulation (PIM) fault or damaged jumper.
- Report suspected external interference to the national regulator if confirmed.
