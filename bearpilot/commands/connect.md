---
description: Connect to BlueBEAR — pin one login node and probe the live cluster ground-truth
argument-hint: "[--stop]"
allowed-tools: Bash
---

Open (or, with `--stop`, close) a pinned SSH connection to the BlueBEAR cluster and report the
live ground-truth.

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/harness/bb-connect.sh" $ARGUMENTS
```

Then:
- Confirm the connection succeeded (user `your-username`, key auth, VPN if off-campus).
- Compare the **live** account / QoS / GRES / CUDA-module output against the encoded defaults
  the script prints. If anything differs (a renamed QoS, a rotated CUDA module, a moved RDS
  path), say so explicitly and note that `references/cluster-ground-truth.md` should be
  re-pinned before building jobs — do not silently trust the stale value.
- If the connection fails, walk the user through the checklist in `references/gotchas.md`
  (items 1–3): VPN, key auth, and the `your-username@` username.
