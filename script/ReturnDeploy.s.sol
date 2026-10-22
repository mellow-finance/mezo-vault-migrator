// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

// Mezo -> Ethereum return leg, step 2: fund check, root, claims.
//
// Unlike MigrationDeploy.s.sol (which mints shares on a Mezo vault through PermissionedMinter),
// nothing is minted here: assets are transferred straight to a URD on ethereum and mezo holders
// claim plain ERC20.
//
// Build the input with `npm run return:build`, once the mezo queues are paused and oracle reports stopped:
//   mezo:holders   active-share snapshot of the mezo share managers -> data/<symbol>/mezo_holders.json
//   mezo:contracts contract holders: is it a Safe the mezo signers still control on ethereum -> ethereum_access
//   return:proofs  the URD's actual balance split exactly by snapshot shares (zero amounts dropped), verified,
//                  -> data/<symbol>/mezo_proofs.json; while the URD holds nothing it builds a TEST tree
//                  (amountSource: test_amount) that this script refuses
//   mezo:check     queues paused, nothing pending, the snapshot still matches the chain
//   test:mezo      mezo fork: nothing a holder can do moves the snapshot shares
//   test:return    mainnet fork: deploy, fund, setRoot, claim every leaf through this script
// Rerun it whenever mezo:check reports the snapshot as stale.
//
// Flow: DeployReturnURDs.s.sol (done: RETURN_URD_MBHBTC / RETURN_URD_MBHCBBTC) -> fund each URD with what
// came back from mezo -> npm run return:build -> _checkFunding() -> owner safe executes the
// _logSetRootCalldata() calldata -> run() claims in batches.
//
// Pinned to 0.8.19 because UniversalRewardsDistributor is pinned to 0.8.19, so this file cannot
// import flexible-vaults (0.8.25) — it does not need to, there is no vault step.

import "forge-std/Script.sol";
import "forge-std/StdJson.sol";
import "forge-std/interfaces/IMulticall3.sol";

import {RETURN_URD_MBHBTC, RETURN_URD_MBHCBBTC, RETURN_URD_OWNER} from "./DeployReturnURDs.s.sol";
import {UniversalRewardsDistributor} from "lib/morpho-urd/src/UniversalRewardsDistributor.sol";
import {IERC20} from "lib/openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";

contract ReturnDeploy is Script {
    using stdJson for string;

    IMulticall3 constant MULTICALL3 = IMulticall3(0xcA11bde05977b3631167028862bE2a173976CA11);

    struct ReturnData {
        string symbol;
        UniversalRewardsDistributor urd;
        address rewardToken;
        uint256 totalAssets;
        bytes32 root;
        string proofJson;
        string[] claimKeys;
    }

    function run() external {
        // _checkFunding("mbhBTC");
        // _logSetRootCalldata("mbhBTC");
        // revert("dry run");

        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));

        // ================================================
        string memory symbol = "mbhBTC";
        uint256 batchId = 0;
        uint256 batchSize = 90;
        // ================================================

        ReturnData memory data = _returnData(symbol);
        uint256 start = batchId * batchSize;
        uint256 end = start + batchSize;
        if (end > data.claimKeys.length) {
            end = data.claimKeys.length;
        }
        console2.log("Claiming %s batch for accounts %d to %d", symbol, start, end - 1);

        vm.startBroadcast(deployerPk);
        _claimBatch(data, start, end);
        vm.stopBroadcast();
    }

    /// @notice Verifies the URD is ours and holds exactly what the tree distributes, before the root is set.
    function _checkFunding(string memory symbol) internal view {
        ReturnData memory data = _returnData(symbol);

        uint256 totalFromClaims;
        for (uint256 i = 0; i < data.claimKeys.length; i++) {
            totalFromClaims += data.proofJson.readUint(string.concat(_base(data, i), ".amount"));
        }
        require(totalFromClaims == data.totalAssets, "TOTAL_ASSETS_MISMATCH");
        require(data.urd.owner() == RETURN_URD_OWNER, "URD_OWNER_MISMATCH");

        uint256 balance = IERC20(data.rewardToken).balanceOf(address(data.urd));
        console2.log("%s URD balance %d, required %d", data.symbol, balance, data.totalAssets);
        require(balance == data.totalAssets, "URD_FUNDING_MISMATCH");
    }

    /// @notice OWNER is a safe, so print the calldata instead of broadcasting it.
    function _logSetRootCalldata(string memory symbol) internal view {
        (address target, bytes memory callData) = _setRootCalldata(symbol);
        console2.log("%s setRoot target %s", symbol, target);
        console2.logBytes(callData);
    }

    function _setRootCalldata(string memory symbol) internal view returns (address target, bytes memory callData) {
        ReturnData memory data = _returnData(symbol);
        target = address(data.urd);
        callData = abi.encodeCall(UniversalRewardsDistributor.setRoot, (data.root, bytes32(0)));
    }

    function _claimBatch(ReturnData memory data, uint256 startIdx, uint256 endIdx) internal {
        require(endIdx <= data.claimKeys.length && startIdx < endIdx, "invalid range");
        require(data.urd.root() == data.root, "URD_ROOT_MISMATCH");
        uint256 count = endIdx - startIdx;

        address[] memory accounts = new address[](count);
        uint256[] memory amounts = new uint256[](count);
        uint256[] memory balancesBefore = new uint256[](count);

        IMulticall3.Call3[] memory calls = new IMulticall3.Call3[](count);
        for (uint256 i = 0; i < count; i++) {
            string memory base = _base(data, startIdx + i);
            address account = vm.parseAddress(data.claimKeys[startIdx + i]);
            uint256 claimable = data.proofJson.readUint(string.concat(base, ".amount"));
            bytes32[] memory proof = data.proofJson.readBytes32Array(string.concat(base, ".proof"));
            // the leaf commits to the account, and the URD transfers to that same account
            require(data.proofJson.readAddress(string.concat(base, ".recipient")) == account, "RECIPIENT_MISMATCH");

            accounts[i] = account;
            amounts[i] = claimable;
            balancesBefore[i] = IERC20(data.rewardToken).balanceOf(account);

            calls[i] = IMulticall3.Call3({
                target: address(data.urd),
                allowFailure: false,
                callData: abi.encodeCall(
                    UniversalRewardsDistributor.claim, (account, data.rewardToken, claimable, proof)
                )
            });
        }

        IMulticall3.Result[] memory results = MULTICALL3.aggregate3{gas: 10_000_000}(calls);

        for (uint256 i = 0; i < count; i++) {
            require(results[i].success, string(abi.encodePacked("Claim failed at index ", vm.toString(startIdx + i))));
            // payout tokens are pre-existing holdings, so assert the delta rather than the balance
            uint256 balanceAfter = IERC20(data.rewardToken).balanceOf(accounts[i]);
            require(
                balanceAfter - balancesBefore[i] == amounts[i],
                string(abi.encodePacked("Incorrect payout at index ", vm.toString(startIdx + i)))
            );
        }
    }

    function _returnData(string memory symbol) internal view returns (ReturnData memory data) {
        data.symbol = symbol;
        data.proofJson = vm.readFile(string(abi.encodePacked("data/", symbol, "/mezo_proofs.json")));
        data.urd = UniversalRewardsDistributor(_urdFor(symbol, data.proofJson));
        data.rewardToken = data.proofJson.readAddress("$.rewardToken");
        data.totalAssets = data.proofJson.readUint("$.totalAssets");
        data.root = data.proofJson.readBytes32("$.root");
        data.claimKeys = vm.parseJsonKeys(data.proofJson, "$.claims");
        require(data.claimKeys.length > 0, "NO_CLAIMS");
    }

    /// @notice The URD the tree was built for: only a tree split from the deployed URD's actual balance is accepted.
    function _urdFor(string memory symbol, string memory proofJson) internal view virtual returns (address urd) {
        require(
            keccak256(bytes(proofJson.readString("$.amountSource"))) == keccak256("urd_balance"),
            "TEST_TREE: fund the URD and rebuild the proofs"
        );
        urd = proofJson.readAddress("$.urd");
        require(urd == _deployedUrd(symbol), "URD_MISMATCH: mezo_proofs.json was built for another URD");
    }

    function _deployedUrd(string memory symbol) internal pure returns (address) {
        bytes32 key = keccak256(bytes(symbol));
        if (key == keccak256("mbhBTC")) {
            return RETURN_URD_MBHBTC;
        }
        if (key == keccak256("mbhcbBTC")) {
            return RETURN_URD_MBHCBBTC;
        }
        revert("unknown symbol for the return leg");
    }

    function _base(ReturnData memory data, uint256 index) internal pure returns (string memory) {
        return string.concat('$.claims["', data.claimKeys[index], '"]');
    }
}
