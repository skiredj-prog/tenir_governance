# TENIR-Gov developer quickstart

This source preview uses the TENIR-Gov 5.7.0 runtime. Install from a checkout containing this guide. The published 5.7.0 package predates the quickstart.

## Setup

Use Python 3.10 or later, preferably in a virtual environment. From the repository root:

```sh
python -m pip install -e .
python -m tenir_governance.quickstart preflight
```

## See the benefit within five minutes after setup

```sh
python -m tenir_governance.quickstart run --output booking-demo
python -m tenir_governance.quickstart verify booking-demo
```

Use a fresh output directory for each run. The simulated booking is created and its response is lost. TENIR-Gov preserves UNKNOWN and refuses a repeat dispatch. One synthetic booking exists. A status lookup returns a correlated receipt and resolves the outcome.

To see how the operation stays UNKNOWN when the receipt is unavailable, run:

```sh
python -m tenir_governance.quickstart run --keep-unknown --output booking-unknown
python -m tenir_governance.quickstart verify booking-unknown
```

Open `booking-demo/evidence.json` or `booking-unknown/evidence.json` for the admission, operation state, dispatch count, budget and receipt. Verification checks local consistency and file hashes. The files contain unsigned synthetic evidence. Five minutes is the onboarding target, measured from preflight through evidence inspection.

For your own sandbox, start with the `SyntheticHotel` class in [quickstart.py](../tenir_governance/quickstart.py) and the [provider interface](../tenir_governance/operations/provider.py). Decide how the provider lets you look up the original effect and how it distinguishes uncertainty from confirmed non-execution before configuring dispatch.
