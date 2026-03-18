// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import "forge-std/Script.sol";
import "forge-std/StdJson.sol";
import "lib/morpho-urd/src/UniversalRewardsDistributor.sol";
import {PermissionedMinter} from "lib/flexible-vaults/src/utils/PermissionedMinter.sol";
import {IERC20Metadata} from "lib/openzeppelin-contracts/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IShareModule} from "lib/flexible-vaults/src/interfaces/modules/IShareModule.sol";
import {IShareManager} from "lib/flexible-vaults/src/interfaces/managers/IShareManager.sol";
import {Vault} from "lib/flexible-vaults/src/vaults/Vault.sol";
import {Integration, VaultState} from "lib/flexible-vaults/test/PermissionedBuilderTest.s.sol";
import "lib/openzeppelin-contracts/contracts/utils/Strings.sol";

contract MigrationDeploy is Script, Integration {
    using Strings for string;
    using stdJson for string;

    struct VaultData {
        address mainnetVault;
        address mezoVault;
        address mezoAdmin;
        uint224 shares;
        string symbol;
        UniversalRewardsDistributor urd;
        PermissionedMinter minter;
    }

    /// @notice An array of Vaults
    VaultData[] public vaultsData;

    function run() external {

        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));

        vm.startBroadcast(deployerPk);
        for (uint256 i = 0; i < vaultsData.length; i++) {
            VaultData memory vault = vaultsData[i];
            (vault.urd, vault.minter) = deploy(vault);
            console2.log("Deployed URD at %s and PermissionedMinter at %s for vault %s", address(vault.urd), address(vault.minter), vault.symbol);
            vaultsData[i] = vault;
        }
        vm.stopBroadcast();

        for (uint256 i = 0; i < vaultsData.length; i++) {
            mint(vaultsData[i]);
            randomClaimTest(vaultsData[i]);
        }

        revert("Deployment complete");
    }

    function setUp() external {

        // mbhBTC
        pushVault(VaultData({
            mainnetVault: 0xa8A3De0c5594A09d0cD4C8abc4e3AaB9BaE03F36,
            mezoVault: 0x807D4778abA870e4222904f5b528F68B350cE0E0,
            mezoAdmin: 0xd5aA2D083642e8Dec06a5e930144d0Af5a97496d,
            shares: 100 ether,
            symbol: "mbhBTC",
            urd: UniversalRewardsDistributor(address(0)),
            minter: PermissionedMinter(address(0))
        }));

        // mbhcbBTC
        pushVault(VaultData({
            mainnetVault: 0x63a76a4a94cAB1DD49fcf0d7E3FC53a78AC8Ec5C,
            mezoVault: 0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8,
            mezoAdmin: 0xd5aA2D083642e8Dec06a5e930144d0Af5a97496d,
            shares: 47294203490000000000,
            symbol: "mbhcbBTC",
            urd: UniversalRewardsDistributor(address(0)),
            minter: PermissionedMinter(address(0))
        }));

        // msvUSD
        pushVault(VaultData({
            mainnetVault: 0x7207595E4c18a9A829B9dc868F11F3ADd8FCF626,
            mezoVault: 0x07AFFA6754458f88db83A72859948d9b794E131b,
            mezoAdmin: 0xC6174983e96D508054cE1DBD778bE8F9f8007Ab3,
            shares: 5563512088197205639376125,
            symbol: "msvUSD",
            urd: UniversalRewardsDistributor(address(0)),
            minter: PermissionedMinter(address(0))
        }));
    }

    function pushVault(VaultData memory vault) internal {
        address shareManagerAddr = address(IShareModule(vault.mezoVault).shareManager());
        console2.log("Pushing vault with share manager at %s", shareManagerAddr);
        IERC20Metadata shareManager = IERC20Metadata(shareManagerAddr);
        require(IShareManager(shareManagerAddr).activeShares() == 0, "ACTIVE_SHARES_PRESENT");
        require(IShareManager(shareManagerAddr).totalShares() == 0, "TOTAL_SHARES_PRESENT");
        require(shareManager.symbol().equal(vault.symbol), "SYMBOL_MISMATCH");
        vaultsData.push(vault);
    }

    function deploy(VaultData memory vault) internal returns (UniversalRewardsDistributor, PermissionedMinter) {
        UniversalRewardsDistributor urd = new UniversalRewardsDistributor(vault.mezoAdmin, 0, bytes32(0), bytes32(0));
        PermissionedMinter minter = new PermissionedMinter(Vault(payable(vault.mezoVault)), vault.mezoAdmin, address(urd), vault.shares, 3);
        return (urd, minter);
    }

    function mint(VaultData memory vaultData) internal {
        Vault vault = Vault(payable(vaultData.mezoVault));

        VaultState memory stateBefore = getVaultState(vault);

        vm.startPrank(vaultData.mezoAdmin);
        vault.grantRole(vault.DEFAULT_ADMIN_ROLE(), address(vaultData.minter));
        vaultData.minter.mint();
        vm.stopPrank();

        VaultState memory stateAfter = getVaultState(vault);

        compareVaultStates(stateBefore, stateAfter);

        console2.log(vault.shareManager().sharesOf(address(vaultData.urd)));

        for (uint256 roleIndex = 0; roleIndex < vault.supportedRoles(); roleIndex++) {
            bytes32 role = vault.supportedRoleAt(roleIndex);
            assertFalse(vault.hasRole(role, address(vaultData.minter)), "minter should not have any role");
        }
    }

    function randomClaimTest(VaultData memory vaultData) internal {
        address rewardToken = address(IShareModule(vaultData.mezoVault).shareManager());

        string memory raw = vm.readFile(getProofPath(vaultData.symbol));

        vm.prank(vaultData.mezoAdmin);
        vaultData.urd.setRoot(raw.readBytes32("$.root"), bytes32(0));

        string[] memory claimKeys = vm.parseJsonKeys(raw, "$.claims");
        
        for (uint256 i = 0; i < claimKeys.length; i += 23) {
            address account = vm.parseAddress(claimKeys[i]);

            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            uint256 claimableTotal = raw.readUint(string.concat(base, ".amount"));
            address recipient = raw.readAddress(string.concat(base, ".recipient"));
            bytes32[] memory proof = raw.readBytes32Array(string.concat(base, ".proof"));

            uint256 balBefore = IERC20(rewardToken).balanceOf(account);
            console2.log(
                "Claiming for account %s, claimable total: %d, balance before: %d", account, claimableTotal, balBefore
            );
            // anyone can submit claim; in Morpho URD it’s permissionless
            uint256 paid;
            try vaultData.urd.claim(recipient, rewardToken, claimableTotal, proof) returns (uint256 paid_) {
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
    }

    function getProofPath(string memory symbol) internal pure returns (string memory) {
        return string(abi.encodePacked("data/", symbol, "/proofs.json"));
    }
}