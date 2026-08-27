// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/StdJson.sol";
import "forge-std/Test.sol";
import "lib/morpho-urd/src/UniversalRewardsDistributor.sol";

/// @notice Mezo -> Ethereum return leg: holders claim plain assets, not shares.
///         Run against a mainnet fork; input is data/<symbol>/mezo_proofs.json.
contract ReturnURDTest is Test {
    using stdJson for string;

    string SYMBOL = "mbhBTC";

    address urd_owner = vm.addr(uint256(keccak256("return_urd_owner")));

    function test_claim_all_from_return_json() external {
        string memory raw = vm.readFile(getReturnProofPath());

        address rewardToken = raw.readAddress("$.rewardToken");
        uint256 totalAssets = raw.readUint("$.totalAssets");

        UniversalRewardsDistributor urd =
            new UniversalRewardsDistributor(urd_owner, 0, raw.readBytes32("$.root"), bytes32(0));
        deal(rewardToken, address(urd), totalAssets);

        string[] memory claimKeys = vm.parseJsonKeys(raw, "$.claims");
        require(claimKeys.length > 0, "NO_CLAIMS");

        // the URD is funded with the sum of the floored per-holder amounts, so it must drain exactly
        {
            uint256 total;
            for (uint256 i = 0; i < claimKeys.length; i++) {
                total += raw.readUint(string.concat('$.claims["', claimKeys[i], '"].amount'));
            }
            assertEq(total, totalAssets, "sum of claims != totalAssets");
            assertEq(IERC20(rewardToken).balanceOf(address(urd)), total);
        }

        address prevAccount;

        for (uint256 i = 0; i < claimKeys.length; i++) {
            address account = vm.parseAddress(claimKeys[i]);
            if (account <= prevAccount) {
                revert(string(abi.encodePacked("ACCOUNTS_NOT_SORTED", " at index ", vm.toString(i))));
            }
            prevAccount = account;

            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            uint256 claimable = raw.readUint(string.concat(base, ".amount"));
            bytes32[] memory proof = raw.readBytes32Array(string.concat(base, ".proof"));
            assertEq(raw.readAddress(string.concat(base, ".recipient")), account, "RECIPIENT_MISMATCH");
            assertGt(claimable, 0, "zero-amount leaf should not be in the tree");

            // unlike the forward leg, claimers may already hold the payout token: assert the delta
            uint256 balBefore = IERC20(rewardToken).balanceOf(account);
            uint256 paid;
            try urd.claim(account, rewardToken, claimable, proof) returns (uint256 paid_) {
                paid = paid_;
            } catch Error(string memory reason) {
                revert(string(abi.encodePacked("CLAIM_FAILED: ", reason, " at index ", vm.toString(i))));
            } catch {
                revert(string(abi.encodePacked("CLAIM_FAILED with unknown error at index ", vm.toString(i))));
            }

            assertEq(IERC20(rewardToken).balanceOf(account) - balBefore, paid);
            assertEq(paid, claimable); // first claim; claimed starts at 0
        }

        assertEq(IERC20(rewardToken).balanceOf(address(urd)), 0, "URD did not drain");
    }

    function test_return_double_claim_reverts() external {
        string memory raw = vm.readFile(getReturnProofPath());

        address rewardToken = raw.readAddress("$.rewardToken");
        uint256 totalAssets = raw.readUint("$.totalAssets");

        UniversalRewardsDistributor urd =
            new UniversalRewardsDistributor(urd_owner, 0, raw.readBytes32("$.root"), bytes32(0));
        deal(rewardToken, address(urd), totalAssets);

        string[] memory claimKeys = vm.parseJsonKeys(raw, "$.claims");
        address account = vm.parseAddress(claimKeys[0]);

        string memory base = string.concat('$.claims["', claimKeys[0], '"]');
        uint256 claimable = raw.readUint(string.concat(base, ".amount"));
        bytes32[] memory proof = raw.readBytes32Array(string.concat(base, ".proof"));

        urd.claim(account, rewardToken, claimable, proof);

        vm.expectRevert(bytes("claimable too low")); // ErrorsLib.CLAIMABLE_TOO_LOW
        urd.claim(account, rewardToken, claimable, proof);
    }

    function getReturnProofPath() internal view returns (string memory) {
        return string(abi.encodePacked("data/", SYMBOL, "/mezo_proofs.json"));
    }
}
