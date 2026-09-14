# Initial token allocation

This feature is separate from trading. `WH_TRADING=0` stays off before, during and after launch.

## What happens

1. The operator chooses a launch time and explicitly enables the initial purchase after reviewing funding limits.
2. WORM reads launch supply, fees, USDG curve reserves, launch fee, router wiring and the economics pin from one block. Integer arithmetic quotes at least 2% of launch supply. A quote that could reach the curve’s reserved graduation allocation is rejected.
3. WORM grants the Pons launch router exactly the quoted USDG allowance. It checks the allowance and simulates the actual funded call before signing.
4. `PonsV2LaunchAndBuy.launchAndBuy` creates the token and purchases the initial allocation in one transaction. If the purchase reverts, token creation reverts too. The caller is retained as the token’s deployer. The router exempts the recipient from the launch-time sniper tax; normal curve fees and creator tax still apply.
5. After chain finality, WORM verifies factory, router, supply-mint and purchase-transfer events against the saved intent. Only then does it record the token and submit exactly 1% of launch supply to the saved creator wallet.
6. After the creator transfer finalizes and its event matches, the allocation is complete. WORM retains 1% plus the small rounding remainder from USDG’s six-decimal precision. It does not buy again to rebalance those percentages later.

The creator transfer is a second transaction, not part of the atomic launch-and-buy call. The contracts provide one purchase recipient, not an atomic multi-recipient distribution. If the transfer is delayed or fails, the tokens remain in WORM while the durable record awaits reconciliation. Do not describe the entire three-step approval/launch/transfer workflow as one transaction.

## Configuration

All settings are private deployment configuration. Never put signing keys or operations credentials in the frontend.

| Setting | Purpose |
| --- | --- |
| `WH_LAUNCH_BUY=1` | Opt into this workflow. Default is off. |
| `WH_LAUNCH_BUY_MAX_USDG` | Required explicit maximum purchase amount. A higher quote is rejected. |
| `WH_LAUNCH_KEEP_USDG` | Required positive USDG balance to retain after the purchase, in addition to fee obligations. |
| `WH_LAUNCH_KEEP_ETH` | Required positive ETH reserve to preserve after each transaction, including its maximum gas cost. |
| `WH_OWNER_WALLET` | Creator’s allocation recipient. Must be valid, nonzero, and different from WORM. |
| `WH_LAUNCH_AT` | Optional first-time schedule initialization with an explicit timezone. Authenticated schedule control can also set the time. |

Existing global transaction fee/value limits also apply. No purchase-cap or reserve default silently authorizes spending. Configure the initial-buy settings before choosing/arming the launch time. The quote-only CLI is available with `WH_LAUNCH_BUY=1 python -m wormhole.launch`; execution uses the authenticated launch/schedule service. The CLI refuses `--live` and `--unpinned` for this feature so there is only one durable execution path.

The saved plan fixes the wallet, creator, metadata, supply, salt, quote, economics pin, spending cap and reserves. Changing environment values does not rewrite an in-progress plan. Turning `WH_LAUNCH_BUY` off does not cancel a saved intent. Set `WH_LIVE=0` or create `payments.paused` to stop further sends. Neither can cancel an already submitted transaction.

## Budget and timing

At the September 14 verification block, the configured supply was one billion tokens, the curve fee was 1%, and creator tax was 2%. The rounded quote was **68.083317 USDG**, yielding approximately **20,000,000.4967 tokens**. The creator receives exactly **10,000,000**, and WORM retains approximately **10,000,000.4967**. This is an observed quote, not a guaranteed future price.

The launch fee was **0.0005 ETH**, with transaction gas additional. Purchase funds are separate from operating runway. For example, a 76.50 USDG reserve plus that purchase requires at least 144.583317 USDG before any other outstanding obligations. The reserve itself is a planned-cost estimate, not a promise of 90 days of service.

Approval confirmation, funding checks, chain inclusion, and finality take time. At the chosen timestamp the server begins the workflow; the visible countdown is not a guarantee of instant token creation. The launch and purchase finalize together. The creator transfer starts afterward and can take another finality interval.

## Recovery and verification limits

- The private application database stores `launch_allocation`; the private transaction outbox stores signed submissions. Back up and restore both together.
- Pending transaction hashes are persisted before broadcast. Recovery sends only original signed bytes. Completed steps are never repeated automatically.
- An unknown receipt remains pending. A confirmed revert or inconsistent evidence requires operator review; it does not trigger another launch or creator transfer.
- Changed economics after approval require a new reviewed plan. A failed simulation can leave an unused exact allowance; editing code does not revoke it.
- The router has no deadline argument. The launch pins economics and minimum quantity, and the sender enforces a 30-second pre-sign quote limit. A pending signed launch has no on-chain expiry; a timer cancellation cannot revoke it. Do not clear its journal or assume that elapsed time cancels it.
- Public launch status reports allocation progress. The private health endpoint flags review states and workflows unresolved for more than one hour. Delivery of application-specific email alerts is a separate operational gate.
- Local failure tests and a fork of current deployed contracts passed. The fork uses disposable accounts and simulated funds; it cannot prove real RPC finality timing, production custody, host durability, or unattended operation. The local fork’s gas pricing differs from Robinhood Chain and uses an explicit test-only fee cap; production fee limits were not changed.

Verified router source: [PonsV2LaunchAndBuy on Robinhood Chain](https://robinhoodchain.blockscout.com/address/0xe33e9e479df8802cb0866d5d05258bec4cf62948?tab=contract).
