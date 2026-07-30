# WorkPulse Classroom — shared-network setup

WorkPulse Classroom has two role-neutral parts:

- **Session Console** — prepares policies, starts or schedules sessions, receives
  device health and policy exceptions.
- **Managed Device Agent** — runs silently on each classroom or lab computer,
  receives the active policy and reports only policy/device-health events.

The devices may use any shared network on which the managed device can reach
the Session Console machine. The network does not need internet access.

## Pair a managed device

1. On the Session Console computer, open WorkPulse → Classroom → Session
   Console.
2. Under **Add a device**, choose **Generate pairing code**.
3. Install the same WorkPulse release on the managed computer.
4. Run the enrolment command shown beside the pairing code. It contains:
   - the Session Console gateway address;
   - a single-use pairing code;
   - the managed device's display name.
5. Start the agent:

   ```text
   workpulse classroom-agent run
   ```

The managed device appears in **Enrolled devices** within five seconds. A green
dot means it has sent a heartbeat in the last 30 seconds.

## Network boundary

The personal WorkPulse dashboard remains bound to `127.0.0.1` and is not
available to classroom devices. Pairing starts a separate gateway on port
`5722`. That gateway exposes only:

- health;
- one-time device enrolment;
- authenticated policy retrieval;
- authenticated heartbeat and policy-event submission.

Each paired device receives its own random bearer token. Pairing codes expire
after 15 minutes and can be used only once.

If the devices are on different subnets, client isolation is enabled, or a
firewall blocks port `5722`, the managed device cannot reach the gateway. An
institution can then allow TCP 5722 between the relevant device VLANs or place
the gateway on a reachable institutional host. The protocol and enrolment flow
remain the same.

## Enforcement boundary

The current Managed Device Agent detects foreground applications and supported
browser destinations, applies the active allowlist locally and reports
exceptions with device identity. Browser-level blocking requires the WorkPulse
browser extension. Until that layer is installed, the Session Console reports
an exception as **detected**, not **blocked**.
