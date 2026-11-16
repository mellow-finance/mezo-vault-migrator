// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

import "forge-std/StdJson.sol";
import "forge-std/Test.sol";
import "forge-std/interfaces/IMulticall3.sol";

import {
    DeployReturnURDs,
    RETURN_URD_MBHBTC,
    RETURN_URD_MBHCBBTC,
    RETURN_URD_OWNER
} from "../script/DeployReturnURDs.s.sol";
import {ReturnDeploy} from "../script/ReturnDeploy.s.sol";
import {UniversalRewardsDistributor} from "lib/morpho-urd/src/UniversalRewardsDistributor.sol";
import {IERC20} from "lib/openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";

interface IBlacklistable {
    function isBlacklisted(address account) external view returns (bool);
}

/// @notice DeployReturnURDs without the broadcast.
contract DeployHarness is DeployReturnURDs {
    function deployURDs() external returns (address mbhBTC, address mbhcbBTC) {
        (UniversalRewardsDistributor a, UniversalRewardsDistributor b) = _deployURDs(RETURN_URD_OWNER);
        return (address(a), address(b));
    }
}

/// @notice ReturnDeploy with the URD addresses injected, so its own code paths run against the fork deployments.
contract ClaimHarness is ReturnDeploy {
    mapping(bytes32 => address) internal urds;

    function setUrd(string memory symbol, address urd) external {
        urds[keccak256(bytes(symbol))] = urd;
    }

    function _urdFor(string memory symbol, string memory) internal view override returns (address urd) {
        urd = urds[keccak256(bytes(symbol))];
        require(urd != address(0), "URD not deployed in the test");
    }

    function checkFunding(string memory symbol) external view {
        _checkFunding(symbol);
    }

    function setRootCalldata(string memory symbol) external view returns (address target, bytes memory callData) {
        return _setRootCalldata(symbol);
    }

    function claimBatch(string memory symbol, uint256 start, uint256 end) external {
        _claimBatch(_returnData(symbol), start, end);
    }
}

/// @notice ReturnDeploy exactly as it is broadcast: the URD comes from mezo_proofs.json.
contract ScriptHarness is ReturnDeploy {
    function urdOf(string memory symbol) external view returns (address) {
        return address(_returnData(symbol).urd);
    }
}

/// @notice Mezo -> Ethereum return leg on a mainnet fork, end to end through both scripts: the deploy script
///         against the URDs already on mainnet (RETURN_URD_*), then those URDs funded, rooted from the owner
///         safe and claimed in batches. Input is data/<symbol>/mezo_proofs.json (`npm run return:build`).
contract ReturnURDTest is Test {
    using stdJson for string;

    uint256 constant BATCH_SIZE = 90; // ReturnDeploy.run() batchSize
    uint256 constant MULTICALL_GAS = 10_000_000; // gas ReturnDeploy._claimBatch forwards to aggregate3
    IMulticall3 constant MULTICALL3 = IMulticall3(0xcA11bde05977b3631167028862bE2a173976CA11);

    ClaimHarness claimer;
    // the URDs on mainnet (RETURN_URD_*) and the ones the deploy script produces in the fork
    mapping(bytes32 => UniversalRewardsDistributor) urds;
    mapping(bytes32 => UniversalRewardsDistributor) fresh;

    function setUp() external {
        (address mbhBTC, address mbhcbBTC) = new DeployHarness().deployURDs();
        fresh[keccak256("mbhBTC")] = UniversalRewardsDistributor(mbhBTC);
        fresh[keccak256("mbhcbBTC")] = UniversalRewardsDistributor(mbhcbBTC);
        urds[keccak256("mbhBTC")] = UniversalRewardsDistributor(RETURN_URD_MBHBTC);
        urds[keccak256("mbhcbBTC")] = UniversalRewardsDistributor(RETURN_URD_MBHCBBTC);

        claimer = new ClaimHarness();
        claimer.setUrd("mbhBTC", RETURN_URD_MBHBTC);
        claimer.setUrd("mbhcbBTC", RETURN_URD_MBHCBBTC);
    }

    function test_deploy() external view {
        UniversalRewardsDistributor mbhBTC = fresh[keccak256("mbhBTC")];
        UniversalRewardsDistributor mbhcbBTC = fresh[keccak256("mbhcbBTC")];
        assertTrue(address(mbhBTC) != address(mbhcbBTC));
        for (uint256 i = 0; i < 2; i++) {
            UniversalRewardsDistributor urd = i == 0 ? mbhBTC : mbhcbBTC;
            assertEq(urd.owner(), RETURN_URD_OWNER);
            assertEq(urd.timelock(), 0);
            assertEq(urd.root(), bytes32(0));
        }
    }

    /// @dev The URDs on mainnet are exactly what the deploy script produces, and nobody but the owner safe can root them.
    function test_deployed_urds() external view {
        string[2] memory symbols = ["mbhBTC", "mbhcbBTC"];
        for (uint256 i = 0; i < symbols.length; i++) {
            bytes32 key = keccak256(bytes(symbols[i]));
            UniversalRewardsDistributor urd = urds[key];
            assertEq(address(urd).code, address(fresh[key]).code, string.concat(symbols[i], ": runtime code"));
            assertEq(urd.owner(), RETURN_URD_OWNER);
            assertEq(urd.timelock(), 0);
            (,, uint256 pendingValidAt) = urd.pendingRoot();
            assertEq(pendingValidAt, 0, "pending root");
            assertFalse(urd.isUpdater(0x4d551d74e851Bd93Ce44D5F588Ba14623249CDda), "hot deployer is an updater");

            string memory raw = vm.readFile(string.concat("data/", symbols[i], "/mezo_proofs.json"));
            assertEq(raw.readAddress("$.urd"), address(urd), string.concat(symbols[i], ": proofs built for another URD"));
            assertTrue(urd.root() == bytes32(0) || urd.root() == raw.readBytes32("$.root"), "unexpected root on mainnet");
            console2.log("%s URD %s", symbols[i], address(urd));
        }
    }

    /// @dev A tree built for a test amount must never reach setRoot or the claims.
    function test_script_refuses_test_tree() external {
        ScriptHarness script = new ScriptHarness();
        string[2] memory symbols = ["mbhBTC", "mbhcbBTC"];
        for (uint256 i = 0; i < symbols.length; i++) {
            string memory raw = vm.readFile(string.concat("data/", symbols[i], "/mezo_proofs.json"));
            if (keccak256(bytes(raw.readString("$.amountSource"))) == keccak256("urd_balance")) {
                assertEq(script.urdOf(symbols[i]), raw.readAddress("$.urd"));
            } else {
                vm.expectRevert(bytes("TEST_TREE: fund the URD and rebuild the proofs"));
                script.urdOf(symbols[i]);
            }
        }
    }

    function test_fund_set_root_claim_mbhBTC() external {
        _fundSetRootClaim("mbhBTC");
    }

    function test_fund_set_root_claim_mbhcbBTC() external {
        _fundSetRootClaim("mbhcbBTC");
    }

    struct Case {
        string symbol;
        string raw;
        UniversalRewardsDistributor urd;
        address rewardToken;
        uint256 totalAssets;
        string[] claimKeys;
        uint256[] balancesBefore; // reward token balance of each claimKeys[i] before the claims
    }

    function _fundSetRootClaim(string memory symbol) internal {
        Case memory c;
        c.symbol = symbol;
        c.raw = vm.readFile(string.concat("data/", symbol, "/mezo_proofs.json"));
        c.urd = urds[keccak256(bytes(symbol))];
        c.rewardToken = c.raw.readAddress("$.rewardToken");
        c.totalAssets = c.raw.readUint("$.totalAssets");
        c.claimKeys = vm.parseJsonKeys(c.raw, "$.claims");

        _fund(c);
        _setRoot(c);
        _claimAll(c);
        _assertDrained(c);
        console2.log("%s: %d claims, %d paid", symbol, c.claimKeys.length, c.totalAssets);
    }

    /// @dev 1. funding: exactly totalAssets, anything else is rejected by ReturnDeploy._checkFunding
    function _fund(Case memory c) internal {
        deal(c.rewardToken, address(c.urd), c.totalAssets + 1);
        vm.expectRevert(bytes("URD_FUNDING_MISMATCH"));
        claimer.checkFunding(c.symbol);
        deal(c.rewardToken, address(c.urd), c.totalAssets - 1);
        vm.expectRevert(bytes("URD_FUNDING_MISMATCH"));
        claimer.checkFunding(c.symbol);
        deal(c.rewardToken, address(c.urd), c.totalAssets);
        assertEq(IERC20(c.rewardToken).balanceOf(address(c.urd)), c.totalAssets, "deal failed");
        claimer.checkFunding(c.symbol);

        if (c.urd.root() == bytes32(0)) {
            (address account, uint256 amount, bytes32[] memory proof) = _claimAt(c.raw, c.claimKeys[0]);
            vm.expectRevert(bytes("root is not set"));
            c.urd.claim(account, c.rewardToken, amount, proof);
            vm.expectRevert(bytes("URD_ROOT_MISMATCH"));
            claimer.claimBatch(c.symbol, 0, 1);
        }
    }

    /// @dev 2. root: the owner safe executes the calldata ReturnDeploy prints, nobody else can
    function _setRoot(Case memory c) internal {
        (address target, bytes memory callData) = claimer.setRootCalldata(c.symbol);
        assertEq(target, address(c.urd));
        (bool ok,) = target.call(callData);
        assertFalse(ok, "non-owner set the root");
        if (c.urd.root() != c.raw.readBytes32("$.root")) {
            vm.prank(RETURN_URD_OWNER);
            (ok,) = target.call(callData);
            assertTrue(ok, "owner setRoot failed");
        }
        assertEq(c.urd.root(), c.raw.readBytes32("$.root"));

        (address account, uint256 amount, bytes32[] memory proof) = _claimAt(c.raw, c.claimKeys[0]);
        vm.expectRevert(bytes("invalid proof or expired"));
        c.urd.claim(account, c.rewardToken, amount + 1, proof);
    }

    /// @dev 3. claims: the same batches ReturnDeploy.run() broadcasts
    function _claimAll(Case memory c) internal {
        // a blacklisted recipient (cbBTC has a blacklist) would revert its whole multicall batch
        for (uint256 i = 0; i < c.claimKeys.length; i++) {
            try IBlacklistable(c.rewardToken).isBlacklisted(vm.parseAddress(c.claimKeys[i])) returns (bool blacklisted) {
                assertFalse(blacklisted, string.concat("blacklisted recipient ", c.claimKeys[i]));
            } catch {
                break; // no blacklist on this token
            }
        }

        c.balancesBefore = new uint256[](c.claimKeys.length);
        for (uint256 i = 0; i < c.claimKeys.length; i++) {
            c.balancesBefore[i] = IERC20(c.rewardToken).balanceOf(vm.parseAddress(c.claimKeys[i]));
        }

        for (uint256 start = 0; start < c.claimKeys.length; start += BATCH_SIZE) {
            uint256 end = start + BATCH_SIZE < c.claimKeys.length ? start + BATCH_SIZE : c.claimKeys.length;
            uint256 gasUsed = _multicallGas(c, start, end);
            assertLt(gasUsed, MULTICALL_GAS, "batch does not fit the multicall gas cap");
            console2.log("%s batch %d: aggregate3 gas %d", c.symbol, start / BATCH_SIZE, gasUsed);
            claimer.claimBatch(c.symbol, start, end);
        }
    }

    /// @dev 4. drained exactly, every recipient received its amount, every leaf marked claimed, nothing can be claimed twice
    function _assertDrained(Case memory c) internal {
        assertEq(IERC20(c.rewardToken).balanceOf(address(c.urd)), 0, "URD did not drain");
        for (uint256 i = 0; i < c.claimKeys.length; i++) {
            address recipient = vm.parseAddress(c.claimKeys[i]);
            uint256 amount = c.raw.readUint(string.concat('$.claims["', c.claimKeys[i], '"].amount'));
            assertEq(c.urd.claimed(recipient, c.rewardToken), amount, "claimed mismatch");
            assertEq(
                IERC20(c.rewardToken).balanceOf(recipient) - c.balancesBefore[i],
                amount,
                string.concat("recipient balance ", c.claimKeys[i])
            );
        }

        (address account, uint256 firstAmount, bytes32[] memory proof) = _claimAt(c.raw, c.claimKeys[0]);
        vm.expectRevert(bytes("claimable too low"));
        c.urd.claim(account, c.rewardToken, firstAmount, proof);
        vm.expectRevert();
        claimer.claimBatch(c.symbol, 0, c.claimKeys.length < BATCH_SIZE ? c.claimKeys.length : BATCH_SIZE);
    }

    /// @dev Gas of the aggregate3 call alone (the harness also spends gas on JSON parsing), on a throwaway state.
    function _multicallGas(Case memory c, uint256 start, uint256 end) internal returns (uint256 gasUsed) {
        IMulticall3.Call3[] memory calls = new IMulticall3.Call3[](end - start);
        for (uint256 i = start; i < end; i++) {
            (address account, uint256 amount, bytes32[] memory proof) = _claimAt(c.raw, c.claimKeys[i]);
            calls[i - start] = IMulticall3.Call3({
                target: address(c.urd),
                allowFailure: false,
                callData: abi.encodeCall(UniversalRewardsDistributor.claim, (account, c.rewardToken, amount, proof))
            });
        }
        uint256 snapshot = vm.snapshotState();
        uint256 gasBefore = gasleft();
        MULTICALL3.aggregate3{gas: MULTICALL_GAS}(calls);
        gasUsed = gasBefore - gasleft();
        vm.revertToState(snapshot);
    }

    function _claimAt(string memory raw, string memory key)
        internal
        pure
        returns (address account, uint256 amount, bytes32[] memory proof)
    {
        string memory base = string.concat('$.claims["', key, '"]');
        account = vm.parseAddress(key);
        amount = raw.readUint(string.concat(base, ".amount"));
        proof = raw.readBytes32Array(string.concat(base, ".proof"));
    }
}
