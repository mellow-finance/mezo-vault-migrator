# mezo-migrator

Merkle-distribution tooling that moves Mellow vault positions between Ethereum and Mezo.

- **Forward leg (done, March 2026):** Ethereum → Mezo. Holders of mbhBTC, mbhcbBTC and msvUSD on Ethereum
  claimed the same shares on Mezo from Morpho URDs funded by `PermissionedMinter`.
- **Return leg (in progress):** Mezo → Ethereum. Holders of mbhBTC and mbhcbBTC on Mezo claim plain assets on
  Ethereum: tBTC for mbhBTC and cbBTC for mbhcbBTC, from one Morpho
  [UniversalRewardsDistributor](lib/morpho-urd/src/UniversalRewardsDistributor.sol) per vault.

## Layout

| Path | What it is |
|---|---|
| `build_proofs.py` | Snapshots, readiness checks, Merkle trees and proofs (both legs) |
| `script/DeployReturnURDs.s.sol` | Return leg, step 1: deploys the two Ethereum URDs (already done) |
| `script/ReturnDeploy.s.sol` | Return leg, step 2: funding check, `setRoot` calldata for the Safe, batched claims |
| `script/MigrationDeploy.s.sol` | Forward leg: Mezo URDs, `PermissionedMinter`, batched claims |
| `test/MezoFreezeTest.t.sol` | Mezo fork: once queues are paused, nothing a holder can do moves the snapshot shares |
| `test/ReturnURDTest.t.sol` | Ethereum fork: the deployed URDs, funded, rooted by the Safe and fully claimed through the scripts |
| `test/URDTest.t.sol` | Forward leg claims |
| `data/<symbol>/` | `holders.json` / `proofs.json` (forward leg), `mezo_holders.json` / `mezo_proofs.json` (return leg) |
| `fee_recalc.py` | One-off fee analysis: time-weighted cumulative shares from `Mint` events |

## Setup

- [Foundry](https://book.getfoundry.sh/) (`forge`, `cast`).
- Python 3.12. Run `npm run setup` to create `.venv` (git-ignored) and install `requirements.txt` into it.
  - Every Python npm script runs `.venv/bin/python`, so the venv never has to be activated.
  - To run `build_proofs.py` by hand, use `.venv/bin/python build_proofs.py ...` or `source .venv/bin/activate` first.
- `npm` is only used as a task runner (`npm run <script>`). There are no JS dependencies.
- `git submodule update --init --recursive`.
  - `lib/forge-std` is listed in `.gitmodules` but is not committed as a submodule. Add it with `forge install foundry-rs/forge-std@v1.15.0`.
- `.env`:

| Variable | Used for |
|---|---|
| `RPC_URL` | Ethereum mainnet |
| `MEZO_RPC` | Mezo mainnet (chain 31612). Must be an **archive** node: `mezo-contracts` reads historical code |
| `HOT_DEPLOYER` | Private key that broadcasts deployments and claims. It never owns anything |
| `ETHERSCAN_API_KEY` | `--verify` and `mezo-diff` |

## Return leg (Mezo → Ethereum)

### Contracts

| | mbhBTC | mbhcbBTC |
|---|---|---|
| Mezo vault | `0x807D4778abA870e4222904f5b528F68B350cE0E0` | `0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8` |
| Mezo share manager | `0xE2232789D4cF5bb1ffaDA1a105Cc59B18d639318` | `0x8FB0EB4BB6CA5cf3883E83734BD5bD77a87CC20E` |
| Ethereum URD | `0xCf4cBB7994e75B7F8e7F7A52e880860bEaA7eE72` | `0x0746C70Ec99248Ccc1d6e29010e03C9fa5503653` |
| Payout token | tBTC `0x18084fbA666a33d37592fA2633fD49a74DD93a88` (18 dec) | cbBTC `0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf` (8 dec) |

Both URDs have the same settings:
- Owner `0xd5aA2D083642e8Dec06a5e930144d0Af5a97496d`. This is a 3/5 Safe with the same owners on Mezo and Ethereum, and the same Safe is admin of the Mezo vaults.
- No timelock and no root updaters.
- Deployed by the hot deployer `0x4d551d74…` at blocks 26226487 and 26226488.
- The addresses are pinned in `DeployReturnURDs.s.sol` (`RETURN_URD_*`) and in `build_proofs.py` (`eth_urd`). The deploy script refuses to deploy again.

### What gets paid

**Who is in the snapshot.** It holds only **active shares** (`balanceOf`) of every holder on the Mezo share manager. Two kinds of shares are left out because they are settled on Mezo:
- claimable deposit shares (`allocatedShares`), which depositors claim on Mezo;
- shares locked by pending redeem requests (the share manager's own balance), which are redeemed on Mezo. They are recorded under `excludedPendingRedeem`.

**How the payout is split.** The payout is whatever the URD actually holds.
- Each holder gets `floor(balance × shares / totalShares)`.
- The few units left over go one each to the largest fractional parts.
- The amounts therefore add up to the URD balance exactly, and the URD ends at 0 after the claims.
- Holders whose amount is 0 are dropped (`droppedZeroAmount`).

**Test trees.** While a URD holds nothing there is no real tree:
- `return-proofs` builds a **test tree** for `--test-amount` (in token units), or for the oracle estimate if no amount is given;
- it marks the file `amountSource: test_amount`;
- `ReturnDeploy` refuses that tree.

**Verification.** Every build is checked right after it is written: every proof reaches the root, the amounts are the exact pro-rata split, and each holder is either claimed or dropped, never both.

**Contract holders.** `mezo-contracts` labels every contract holder with `ethereum_access`. The URD pays the same address on Ethereum, so the question is who controls that address there:

| `ethereum_access` | Meaning |
|---|---|
| `safe_deployed` | A Safe exists at that address on Ethereum, and its current Mezo signers reach its threshold |
| `safe_deployable` | No code on Ethereum yet, but replaying the Mezo `createProxyWithNonce` (same factory, singleton, initializer and salt) deploys there. The initial owners must reach the initial threshold using only current Mezo signers |
| `safe_no_quorum` | Deployed or deployable, but the current Mezo signers cannot reach the threshold |
| `safe_not_deployable` | No code on Ethereum and the creation cannot be replayed (non-standard factory, different proxy code, `setup` reverts). The reason is recorded |
| `address_taken` | Something other than a Safe sits at that address on Ethereum |
| `not_safe` | Not a Safe on Mezo: needs a manual decision |

Only `safe_deployed` and `safe_deployable` are safe to pay. `return-proofs` prints a warning for every other value.

### Procedure

**1. Freeze the Mezo vaults.**
   - Pause every deposit and redeem queue: `setQueueStatus(queue, true)` from the admin Safe.
   - Stop the oracle reporters. Each report mints fees and burns locked shares, so it changes the snapshot.
   - Check with `npm run mezo:check`. It exits non-zero unless both vaults are `READY`.

**2. Fund each URD** with what came back from Mezo (tBTC / cbBTC). Any amount works: the tree is built from the balance.

**3. Build the proofs:** `npm run return:build`. It runs these steps in order:

| Step | Writes / checks |
|---|---|
| `mezo:holders` | `data/<symbol>/mezo_holders.json`: active-share snapshot at the latest block. Fails unless `activeShares == snapshot + locked` |
| `mezo:contracts` | Adds `ethereum_access` and `safe` details to every contract holder |
| `return:proofs` | `data/<symbol>/mezo_proofs.json`: URD balance split by shares, URD Merkle tree, verified |
| `mezo:check` | Queues paused, nothing pending, and the snapshot still matches the chain |
| `test:mezo` | Mezo fork: no holder action moves the snapshot |
| `test:return` | Ethereum fork: the deployed URDs funded, rooted and fully claimed through the scripts |

Then check the output:
- `amountSource: urd_balance`, not `test_amount`;
- the `verified:` line;
- no `ethereum_access` warnings.

Rerun the whole build whenever `mezo:check` reports the snapshot as stale, or when the URD balance changes.

**4. Check the funding and print the root calldata.**
   - In `ReturnDeploy.run()`, uncomment `_checkFunding(symbol)`, `_logSetRootCalldata(symbol)` and `revert("dry run")`.
   - Run without broadcasting:
     ```sh
     set -a; source .env; set +a
     forge script ./script/ReturnDeploy.s.sol:ReturnDeploy --fork-url $RPC_URL
     ```
   - `_checkFunding` requires that the URD is the pinned one, its owner is the Safe, and its balance equals `totalAssets`.

**5. Set the root.** The owner Safe executes the printed `setRoot(root, 0)` calldata on each URD.

**6. Claim.**
   - Set `symbol` and `batchId` in `ReturnDeploy.run()`, then run `npm run deploy:return` once per batch. This **broadcasts**.
   - Batches are 90 claims through Multicall3, at about 5.6M gas each: 271 claims make batches 0–3 for mbhBTC, and 72 claims make batch 0 for mbhcbBTC (current snapshot).
   - Claims are permissionless and pay each holder's own address. A batch reverts as a whole if any claim fails.

**7. Confirm.** Each URD balance is 0, and `claimed(account, token)` equals every leaf amount.

### Data files

`mezo_holders.json`:
- `holders`: `{address: {shares, recipient, type, code, ethereum_access?, safe?}}`;
- totals: `totalShares`, `activeShares`, `snapshotShares`, `excludedAllocatedShares`;
- `excludedPendingRedeem`: the total, plus shares per requester.

`mezo_proofs.json`:
- source: `amountSource`, `urd`, `rewardToken`;
- totals: `totalShares`, `totalAssets` (the amount the URD must hold), `droppedZeroAmount`;
- tree: `root`, and `claims` as `{address: {shares, amount, proof, type, recipient, ethereum_access?}}`;
- for reference only: `oracleEstimate` and the Mezo oracle report.
- Leaf: `keccak256(bytes.concat(keccak256(abi.encode(account, rewardToken, claimable))))`, with sorted-pair hashing as in OpenZeppelin `MerkleProof`.

### Security notes

**The URD owner can redirect the funds.** In the URD, `setRoot` from the owner bypasses any timelock. The owner Safe could therefore set a root that pays someone else.
- This adds no new trust: the same Safe administers the Mezo vaults.
- Keep the fund → `setRoot` → claim sequence short.
- `setOwner(address(0))` after `setRoot` would freeze the root for good. Any claim that cannot go through would then leave its funds stuck.

**`ReturnDeploy` refuses dangerous inputs:**
- a test tree;
- proofs built for a URD other than the pinned one;
- claims while the URD root differs from the file.

**The hot deployer cannot set roots.** It is not the owner and not an updater.

## Forward leg (Ethereum → Mezo, completed)

- `npm run holders` snapshots the Ethereum share managers into `data/<symbol>/holders.json`.
- `npm run proofs` builds `proofs.json`.
- `script/MigrationDeploy.s.sol` minted the shares on Mezo into URDs through `PermissionedMinter` and ran the batched claims:
  - mbhBTC `0xF73C154c1F57329A2c9C04C406470E8be14B0c3C`;
  - mbhcbBTC `0x9F947e74b3DB089f480c7E2cA914777f6b07D286`;
  - msvUSD `0x1A7ADC1d931D400B69f3CEd0C91B62d1af0B0712`.
- `npm run mezo:diff` compares the Mezo and Ethereum holder sets. It writes `mezo_only.json` and `mezo_untouched.json`.

## npm scripts

| Script | Command |
|---|---|
| `mezo:check` | Readiness of the Mezo vaults: queues, pending redeems and deposits, oracle, fees, roles, positions, snapshot freshness |
| `mezo:holders` / `mezo:contracts` / `return:proofs` | Return-leg build steps (see above). `return:proofs` also takes `--test-amount` when run directly through `build_proofs.py` |
| `return:build` | The whole return-leg build, in order |
| `test:mezo` / `test:return` | Fork tests (Mezo / Ethereum) |
| `deploy:return:urds` | Deploys the return URDs. Refuses: they are already deployed |
| `deploy:return` | Broadcasts one claim batch of `ReturnDeploy.run()` |
| `holders` / `proofs` / `deploy` / `test` / `mezo:diff` | Forward leg |
