// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import "forge-std/StdJson.sol";
import "forge-std/Test.sol";

import {IFeeManager} from "lib/flexible-vaults/src/interfaces/managers/IFeeManager.sol";
import {IShareManager} from "lib/flexible-vaults/src/interfaces/managers/IShareManager.sol";
import {IACLModule} from "lib/flexible-vaults/src/interfaces/modules/IACLModule.sol";
import {IShareModule} from "lib/flexible-vaults/src/interfaces/modules/IShareModule.sol";
import {IOracle} from "lib/flexible-vaults/src/interfaces/oracles/IOracle.sol";
import {IDepositQueue} from "lib/flexible-vaults/src/interfaces/queues/IDepositQueue.sol";
import {IQueue} from "lib/flexible-vaults/src/interfaces/queues/IQueue.sol";
import {IRedeemQueue} from "lib/flexible-vaults/src/interfaces/queues/IRedeemQueue.sol";

interface IShareToken {
    function transfer(address to, uint256 value) external returns (bool);
}

/// @notice Mezo fork. Once every queue is paused, nothing a holder can do moves `sharesOf`;
///         a final oracle report still settles the locked redeem shares with the queues paused.
///         Input is data/<symbol>/mezo_holders.json (`python3 build_proofs.py mezo-holders all`).
contract MezoFreezeTest is Test {
    using stdJson for string;

    address constant MBHBTC_VAULT = 0x807D4778abA870e4222904f5b528F68B350cE0E0;
    address constant MBHCBBTC_VAULT = 0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8;
    address constant NATIVE = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE;

    struct Queue {
        address queue;
        address asset;
        bool isDeposit;
    }

    function test_frozen_mbhBTC() external {
        _checkFrozen("mbhBTC", MBHBTC_VAULT);
    }

    function test_frozen_mbhcbBTC() external {
        _checkFrozen("mbhcbBTC", MBHCBBTC_VAULT);
    }

    struct ReportCase {
        IShareModule module;
        IShareManager shareManager;
        IOracle oracle;
        IRedeemQueue redeemQueue;
        address recipient;
        uint224 priceD18;
        uint256 locked;
        uint256 expectedFee;
        uint256 totalBefore;
        uint256 recipientBefore;
        uint256 batchesBefore;
    }

    /// @notice Answers "does pausing the queues block the pending redeems": it does not.
    function test_final_report_with_paused_queues_mbhBTC() external {
        IShareManager shareManager = IShareModule(MBHBTC_VAULT).shareManager();
        if (shareManager.activeSharesOf(address(shareManager)) == 0) {
            console2.log("no pending redeems locked in the share manager: nothing for a final report to settle");
            vm.skip(true);
        }
        ReportCase memory c = _reportCase(MBHBTC_VAULT);
        address[] memory accounts = _accounts("mbhBTC");
        uint256[] memory sharesBefore = _sharesOf(c.shareManager, accounts);

        IOracle.Report[] memory reports = new IOracle.Report[](1);
        reports[0] = IOracle.Report({asset: NATIVE, priceD18: c.priceD18});
        vm.prank(IACLModule(address(c.module)).getRoleMember(c.oracle.SUBMIT_REPORTS_ROLE(), 0));
        c.oracle.submitReports(reports);

        uint256 demand = _assertSettled(c);
        for (uint256 i = 0; i < accounts.length; i++) {
            if (accounts[i] != c.recipient) {
                assertEq(c.shareManager.sharesOf(accounts[i]), sharesBefore[i], "holder shares moved on report");
            }
        }
        console2.log("final report: burned %d locked shares, minted %d fee shares", c.locked, c.expectedFee);

        _fundAndClaim(c, demand, _largestRequester("mbhBTC"));
    }

    function _reportCase(address vault) internal returns (ReportCase memory c) {
        c.module = IShareModule(vault);
        c.shareManager = c.module.shareManager();
        c.oracle = c.module.oracle();
        Queue[] memory queues = _queues(c.module);
        _pauseAll(c.module, queues);
        c.redeemQueue = IRedeemQueue(_redeemQueue(queues, NATIVE));

        c.locked = c.shareManager.activeSharesOf(address(c.shareManager));
        assertGt(c.locked, 0, "no pending redeems to settle");
        uint256 pending;
        (, c.batchesBefore,, pending) = c.redeemQueue.getState();
        assertEq(pending, c.locked, "locked shares != totalPendingShares");

        // every request, including the latest one, falls behind the redeem cutoff
        vm.warp(block.timestamp + c.oracle.securityParams().redeemInterval + 1 days);

        IFeeManager feeManager = c.module.feeManager();
        c.priceD18 = c.oracle.getReport(NATIVE).priceD18;
        c.recipient = feeManager.feeRecipient();
        c.totalBefore = c.shareManager.totalShares();
        c.recipientBefore = c.shareManager.sharesOf(c.recipient);
        c.expectedFee = feeManager.calculateFee(
            vault, NATIVE, c.priceD18, c.totalBefore - c.shareManager.activeSharesOf(c.recipient)
        );
    }

    function _assertSettled(ReportCase memory c) internal view returns (uint256 demand) {
        assertFalse(c.oracle.getReport(NATIVE).isSuspicious, "report must be applied directly");
        assertEq(c.shareManager.activeSharesOf(address(c.shareManager)), 0, "locked shares not burned");
        uint256 batches;
        uint256 pending;
        (, batches, demand, pending) = c.redeemQueue.getState();
        assertEq(pending, 0, "pending shares left");
        assertEq(batches, c.batchesBefore + 1, "no new batch");
        assertEq(demand, c.locked * 1 ether / c.priceD18, "batch demand");
        assertEq(c.shareManager.sharesOf(c.recipient), c.recipientBefore + c.expectedFee, "fee shares");
        assertEq(c.shareManager.totalShares(), c.totalBefore - c.locked + c.expectedFee, "totalShares");
    }

    function _fundAndClaim(ReportCase memory c, uint256 demand, address requester) internal {
        vm.prank(address(c.redeemQueue));
        uint256 liquid = c.module.getLiquidAssets();
        console2.log("batch demand %d wei BTC, liquid %d wei BTC", demand, liquid);
        if (liquid < demand) {
            assertEq(c.redeemQueue.handleBatches(1), 0, "unfunded batch must wait for liquidity");
            vm.deal(address(c.module), address(c.module).balance + demand - liquid);
        }
        assertEq(c.redeemQueue.handleBatches(1), 1, "batch not funded");

        // a requester claims BTC on mezo while the queue stays paused
        uint32[] memory timestamps = _claimableTimestamps(c.redeemQueue, requester);
        assertGt(timestamps.length, 0, "requester has nothing claimable");
        uint256 balanceBefore = requester.balance;
        vm.prank(requester);
        uint256 assets = c.redeemQueue.claim(requester, timestamps);
        assertGt(assets, 0, "nothing paid");
        assertEq(requester.balance - balanceBefore, assets, "payout");
        console2.log("requester %s claimed %d wei BTC on mezo", requester, assets);
    }

    function _checkFrozen(string memory symbol, address vault) internal {
        IShareModule module = IShareModule(vault);
        IShareManager shareManager = module.shareManager();
        Queue[] memory queues = _queues(module);
        _pauseAll(module, queues);

        address[] memory accounts = _accounts(symbol);
        uint256[] memory sharesBefore = _sharesOf(shareManager, accounts);
        uint256[] memory activeBefore = _activeSharesOf(shareManager, accounts);
        uint256 totalBefore = shareManager.totalShares();
        uint256 lockedBefore = shareManager.activeSharesOf(address(shareManager));

        vm.warp(block.timestamp + 30 days);
        vm.roll(block.number + 1_000_000);

        for (uint256 i = 0; i < accounts.length; i++) {
            _tryEverything(shareManager, queues, accounts[i], sharesBefore[i]);
        }
        for (uint256 j = 0; j < queues.length; j++) {
            if (!queues[j].isDeposit) {
                IRedeemQueue(queues[j].queue).handleBatches(type(uint256).max);
            }
        }

        // the payout uses sharesOf (active + claimable); a holder's own deposit claim only turns claimable into
        // active, so sharesOf stays put and the active balance grows by exactly the claimable part
        for (uint256 i = 0; i < accounts.length; i++) {
            string memory account = vm.toString(accounts[i]);
            assertEq(shareManager.sharesOf(accounts[i]), sharesBefore[i], string.concat("sharesOf moved for ", account));
            assertEq(
                shareManager.activeSharesOf(accounts[i]),
                sharesBefore[i],
                string.concat("active shares moved beyond own claimables for ", account)
            );
            if (activeBefore[i] != sharesBefore[i]) {
                console2.log("%s claimed its own claimable shares on mezo", accounts[i]);
            }
        }
        assertEq(shareManager.totalShares(), totalBefore, "totalShares moved");
        assertEq(shareManager.activeSharesOf(address(shareManager)), lockedBefore, "locked shares moved");
        console2.log("%s frozen: %d accounts, totalShares %d", symbol, accounts.length, totalBefore);
    }

    /// @dev Every permissionless entry point a holder has: the share-changing ones must revert,
    ///      the rest (claims, batch funding) may succeed but must leave `sharesOf` alone.
    function _tryEverything(IShareManager shareManager, Queue[] memory queues, address account, uint256 shares)
        internal
    {
        for (uint256 j = 0; j < queues.length; j++) {
            Queue memory q = queues[j];
            if (q.isDeposit) {
                vm.deal(account, 1 ether);
                vm.prank(account);
                vm.expectRevert(IQueue.QueuePaused.selector);
                IDepositQueue(q.queue).deposit{value: q.asset == NATIVE ? 1 : 0}(1, address(0), new bytes32[](0));
                IDepositQueue(q.queue).claim(account);
            } else {
                vm.prank(account);
                vm.expectRevert(IQueue.QueuePaused.selector);
                IRedeemQueue(q.queue).redeem(1);
                uint32[] memory timestamps = _claimableTimestamps(IRedeemQueue(q.queue), account);
                if (timestamps.length > 0) {
                    vm.prank(account);
                    IRedeemQueue(q.queue).claim(account, timestamps);
                }
            }
        }
        if (shares > 0) {
            vm.prank(account);
            vm.expectRevert(IShareManager.TransferPaused.selector);
            // forge-lint: disable-next-line(erc20-unchecked-transfer)
            IShareToken(address(shareManager)).transfer(address(0xdead), 1);
        }
        shareManager.claimShares(account);
    }

    function _queues(IShareModule module) internal view returns (Queue[] memory queues) {
        queues = new Queue[](module.getQueueCount());
        uint256 k;
        for (uint256 i = 0; i < module.getAssetCount(); i++) {
            address asset = module.assetAt(i);
            for (uint256 j = 0; j < module.getQueueCount(asset); j++) {
                address queue = module.queueAt(asset, j);
                queues[k++] = Queue({queue: queue, asset: asset, isDeposit: module.isDepositQueue(queue)});
            }
        }
        require(k == queues.length, "queue count mismatch");
    }

    function _pauseAll(IShareModule module, Queue[] memory queues) internal {
        address admin = IACLModule(address(module)).getRoleMember(module.SET_QUEUE_STATUS_ROLE(), 0);
        for (uint256 i = 0; i < queues.length; i++) {
            if (!module.isPausedQueue(queues[i].queue)) {
                console2.log("pausing queue %s as %s", queues[i].queue, admin);
                vm.prank(admin);
                module.setQueueStatus(queues[i].queue, true);
            }
            assertTrue(module.isPausedQueue(queues[i].queue));
        }
    }

    function _redeemQueue(Queue[] memory queues, address asset) internal pure returns (address) {
        for (uint256 i = 0; i < queues.length; i++) {
            if (!queues[i].isDeposit && queues[i].asset == asset) {
                return queues[i].queue;
            }
        }
        revert("no redeem queue for asset");
    }

    function _claimableTimestamps(IRedeemQueue queue, address account) internal view returns (uint32[] memory out) {
        IRedeemQueue.Request[] memory requests = queue.requestsOf(account, 0, 1000);
        uint256 n;
        for (uint256 i = 0; i < requests.length; i++) {
            if (requests[i].isClaimable) {
                n++;
            }
        }
        out = new uint32[](n);
        n = 0;
        for (uint256 i = 0; i < requests.length; i++) {
            if (requests[i].isClaimable) {
                out[n++] = uint32(requests[i].timestamp);
            }
        }
    }

    function _sharesOf(IShareManager shareManager, address[] memory accounts)
        internal
        view
        returns (uint256[] memory shares)
    {
        shares = new uint256[](accounts.length);
        for (uint256 i = 0; i < accounts.length; i++) {
            shares[i] = shareManager.sharesOf(accounts[i]);
        }
    }

    function _activeSharesOf(IShareManager shareManager, address[] memory accounts)
        internal
        view
        returns (uint256[] memory shares)
    {
        shares = new uint256[](accounts.length);
        for (uint256 i = 0; i < accounts.length; i++) {
            shares[i] = shareManager.activeSharesOf(accounts[i]);
        }
    }

    /// @dev Snapshot holders plus the requesters whose shares are locked (excluded from the payout).
    function _accounts(string memory symbol) internal view returns (address[] memory accounts) {
        string memory raw = vm.readFile(_holdersPath(symbol));
        string[] memory holders = vm.parseJsonKeys(raw, "$.holders");
        string[] memory requesters;
        if (raw.readUint("$.excludedPendingRedeem.total") > 0) {
            requesters = vm.parseJsonKeys(raw, "$.excludedPendingRedeem.requesters");
        }
        accounts = new address[](holders.length + requesters.length);
        for (uint256 i = 0; i < holders.length; i++) {
            accounts[i] = vm.parseAddress(holders[i]);
        }
        for (uint256 i = 0; i < requesters.length; i++) {
            accounts[holders.length + i] = vm.parseAddress(requesters[i]);
        }
    }

    function _largestRequester(string memory symbol) internal view returns (address requester) {
        string memory raw = vm.readFile(_holdersPath(symbol));
        string[] memory keys = vm.parseJsonKeys(raw, "$.excludedPendingRedeem.requesters");
        uint256 best;
        for (uint256 i = 0; i < keys.length; i++) {
            uint256 shares = raw.readUint(string.concat('$.excludedPendingRedeem.requesters["', keys[i], '"]'));
            if (shares > best) {
                best = shares;
                requester = vm.parseAddress(keys[i]);
            }
        }
    }

    function _holdersPath(string memory symbol) internal pure returns (string memory) {
        return string.concat("data/", symbol, "/mezo_holders.json");
    }
}
