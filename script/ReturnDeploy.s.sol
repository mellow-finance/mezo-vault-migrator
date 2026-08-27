// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

// Mezo -> Ethereum return leg.
//
// Unlike MigrationDeploy.s.sol (which mints shares on a Mezo vault through PermissionedMinter),
// nothing is minted here: assets are transferred straight to a URD on ethereum and mezo holders
// claim plain ERC20. Build the input with `python3 build_proofs.py return-proofs all`.
//
// Pinned to 0.8.19 because UniversalRewardsDistributor is pinned to 0.8.19, so this file cannot
// import flexible-vaults (0.8.25) — it does not need to, there is no vault step.

import "forge-std/Script.sol";
import "forge-std/StdJson.sol";
import "forge-std/interfaces/IMulticall3.sol";

import {UniversalRewardsDistributor} from "lib/morpho-urd/src/UniversalRewardsDistributor.sol";
import {IERC20} from "lib/openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";

contract ReturnDeploy is Script {
    using stdJson for string;

    IMulticall3 constant MULTICALL3 = IMulticall3(0xcA11bde05977b3631167028862bE2a173976CA11);

    // TODO: owner of the ethereum URDs (the safe that will set roots). Must be set before broadcasting.
    address constant OWNER = address(0);

    // TODO: fill in with the addresses logged by _deployURDs().
    address constant URD_MBHBTC = address(0);
    address constant URD_MBHCBBTC = address(0);

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
        // _deployURDs();
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

    function _deployURDs() internal {
        require(OWNER != address(0), "OWNER not set");

        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));
        vm.startBroadcast(deployerPk);
        {
            UniversalRewardsDistributor urd = new UniversalRewardsDistributor(OWNER, 0, bytes32(0), bytes32(0));
            console2.log("return URD mbhBTC %s", address(urd));
        }
        {
            UniversalRewardsDistributor urd = new UniversalRewardsDistributor(OWNER, 0, bytes32(0), bytes32(0));
            console2.log("return URD mbhcbBTC %s", address(urd));
        }
        vm.stopBroadcast();
    }

    /// @notice Verifies the URD holds exactly what the tree distributes, before any claim runs.
    function _checkFunding(string memory symbol) internal view {
        ReturnData memory data = _returnData(symbol);

        uint256 totalFromClaims;
        for (uint256 i = 0; i < data.claimKeys.length; i++) {
            totalFromClaims += data.proofJson.readUint(string.concat(_base(data, i), ".amount"));
        }
        require(totalFromClaims == data.totalAssets, "TOTAL_ASSETS_MISMATCH");

        uint256 balance = IERC20(data.rewardToken).balanceOf(address(data.urd));
        console2.log("%s URD balance %d, required %d", data.symbol, balance, data.totalAssets);
        require(balance == data.totalAssets, "URD_FUNDING_MISMATCH");
    }

    /// @notice OWNER is expected to be a safe, so print the calldata instead of broadcasting it.
    function _logSetRootCalldata(string memory symbol) internal view {
        ReturnData memory data = _returnData(symbol);
        console2.log("%s setRoot target %s", data.symbol, address(data.urd));
        console2.logBytes(abi.encodeCall(UniversalRewardsDistributor.setRoot, (data.root, bytes32(0))));
    }

    function _claimBatch(ReturnData memory data, uint256 startIdx, uint256 endIdx) internal {
        require(endIdx <= data.claimKeys.length && startIdx < endIdx, "invalid range");
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
        data.urd = UniversalRewardsDistributor(_urdFor(symbol));
        data.proofJson = vm.readFile(string(abi.encodePacked("data/", symbol, "/mezo_proofs.json")));
        data.rewardToken = data.proofJson.readAddress("$.rewardToken");
        data.totalAssets = data.proofJson.readUint("$.totalAssets");
        data.root = data.proofJson.readBytes32("$.root");
        data.claimKeys = vm.parseJsonKeys(data.proofJson, "$.claims");
        require(data.claimKeys.length > 0, "NO_CLAIMS");
    }

    function _urdFor(string memory symbol) internal pure returns (address urd) {
        bytes32 key = keccak256(abi.encodePacked(symbol));
        if (key == keccak256(abi.encodePacked("mbhBTC"))) {
            urd = URD_MBHBTC;
        } else if (key == keccak256(abi.encodePacked("mbhcbBTC"))) {
            urd = URD_MBHCBBTC;
        } else {
            revert("unknown symbol for the return leg");
        }
        require(urd != address(0), "URD address not set: run _deployURDs() first");
    }

    function _base(ReturnData memory data, uint256 index) internal pure returns (string memory) {
        return string.concat('$.claims["', data.claimKeys[index], '"]');
    }
}
