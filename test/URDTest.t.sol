// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/Test.sol";
import "forge-std/StdJson.sol";
import "lib/morpho-urd/src/UniversalRewardsDistributor.sol";
import {IERC20Metadata} from "lib/openzeppelin-contracts/contracts/token/ERC20/extensions/IERC20Metadata.sol";

contract URDTest is Test {
    using stdJson for string;

    address mbhBTC = 0x43f084bdBC99409c637319dD7c544D565165A162;
    address mbhcbBTC = 0x171b8E43bB751A558b2b1f3C814d3c96D36cCf2B;

    address SHARE_MANAGER = mbhcbBTC;

    address urd_owner = vm.addr(uint256(keccak256("urd_owner")));

    function test_claim_all_from_json() external {
        string memory raw = vm.readFile(getProofPath());

        address rewardToken = raw.readAddress("$.rewardToken");
        uint256 totalShares = raw.readUint("$.totalShares");

        // 1) Deploy URD and set root
        UniversalRewardsDistributor urd = new UniversalRewardsDistributor(urd_owner, 0, raw.readBytes32("$.root"), bytes32(0));
        deal(rewardToken, address(urd), totalShares);

        // 3) Read all claim keys under $.claims
        string[] memory claimKeys = vm.parseJsonKeys(raw, "$.claims");
        require(claimKeys.length > 0, "NO_CLAIMS");

        // 4) Compute total to mint to URD
        uint256 total;
        for (uint256 i = 0; i < claimKeys.length; i++) {
            string memory addrStr = claimKeys[i];
            string memory amtPath = string.concat('$.claims["', addrStr, '"].amount');
            uint256 claimableTotal = raw.readUint(amtPath);
            total += claimableTotal;
        }
        assertEq(total, totalShares); // sanity check against total shares
        assertEq(IERC20(rewardToken).balanceOf(address(urd)), total);

        address prevAccount;

        // 5) Claim for everyone
        for (uint256 i = 0; i < claimKeys.length; i++) {
            address account = vm.parseAddress(claimKeys[i]);
            if (account <= prevAccount) {
                revert(string(abi.encodePacked("ACCOUNTS_NOT_SORTED", " at index ", vm.toString(i))));
            }
            prevAccount = account;

            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            uint256 claimableTotal = raw.readUint(string.concat(base, ".amount"));
            bytes32[] memory proof = raw.readBytes32Array(string.concat(base, ".proof"));

            uint256 balBefore = IERC20(rewardToken).balanceOf(account);
            console2.log("Claiming for account %s, claimable total: %d, balance before: %d", account, claimableTotal, balBefore);
            // anyone can submit claim; in Morpho URD it’s permissionless
            uint256 paid;
            try urd.claim(account, rewardToken, claimableTotal, proof) returns (uint256 paid_) {
                paid = paid_;
            } catch Error(string memory reason) {
                revert(string(abi.encodePacked("CLAIM_FAILED: ", reason, " at index ", vm.toString(i))));
            } catch {
                revert(string(abi.encodePacked("CLAIM_FAILED with unknown error at index ", vm.toString(i))));
            }

            uint256 balAfter = IERC20(rewardToken).balanceOf(account);
            assertEq(balAfter - balBefore, paid);
            assertEq(paid, claimableTotal); // because first claim; claimedTotal starts at 0
        }

        // 6) URD should have zero tokens left if totals were correct
        assertEq(IERC20(rewardToken).balanceOf(address(urd)), 0);
    }

    function test_double_claim_reverts() internal {
        string memory raw = vm.readFile(getProofPath());

        address rewardToken = raw.readAddress("$.rewardToken");
        bytes32 root = raw.readBytes32("$.root");
        uint256 totalShares = raw.readUint("$.totalShares");

        UniversalRewardsDistributor urd = new UniversalRewardsDistributor(urd_owner, 0, root, bytes32(0));
        deal(rewardToken, address(urd), totalShares);

        string[] memory claimKeys = vm.parseJsonKeys(raw, "$.claims");
        address account = vm.parseAddress(claimKeys[0]);

        string memory base = string.concat('$.claims["', claimKeys[0], '"]');
        uint256 claimableTotal = raw.readUint(string.concat(base, ".amount"));
        bytes32[] memory proof = raw.readBytes32Array(string.concat(base, ".proof"));

        urd.claim(account, rewardToken, claimableTotal, proof);

        vm.expectRevert("NOTHING_TO_CLAIM");
        urd.claim(account, rewardToken, claimableTotal, proof);
    }
    
    function getProofPath() internal view returns (string memory) {
        string memory symbol = IERC20Metadata(SHARE_MANAGER).symbol();
        return string(abi.encodePacked("data/", symbol, "/proofs.json"));
    }
}