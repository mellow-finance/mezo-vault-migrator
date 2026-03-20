// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import "forge-std/Script.sol";
import "forge-std/StdJson.sol";

import {IShareManager} from "lib/flexible-vaults/src/interfaces/managers/IShareManager.sol";
import {IShareModule} from "lib/flexible-vaults/src/interfaces/modules/IShareModule.sol";
import {PermissionedMinter} from "lib/flexible-vaults/src/utils/PermissionedMinter.sol";

import {Vault} from "lib/flexible-vaults/src/vaults/Vault.sol";
import {Integration, VaultState} from "lib/flexible-vaults/test/PermissionedBuilderTest.s.sol";
import {IERC20} from "lib/openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "lib/openzeppelin-contracts/contracts/token/ERC20/extensions/IERC20Metadata.sol";

interface IUniversalRewardsDistributor {
    function root() external view returns (bytes32);
    function owner() external view returns (address);
    function timelock() external view returns (uint256);
    function ipfsHash() external view returns (bytes32);
    function isUpdater(address) external view returns (bool);
    function claimed(address, address) external view returns (uint256);

    function acceptRoot() external;
    function setRoot(bytes32 newRoot, bytes32 newIpfsHash) external;
    function setTimelock(uint256 newTimelock) external;
    function setRootUpdater(address updater, bool active) external;
    function revokePendingRoot() external;
    function setOwner(address newOwner) external;

    function submitRoot(bytes32 newRoot, bytes32 ipfsHash) external;

    function claim(address account, address reward, uint256 claimable, bytes32[] memory proof)
        external
        returns (uint256 amount);
}

contract MigrationDeploy is Script, Integration {
    using stdJson for string;

    struct VaultData {
        string symbol;
        address mezoVault;
        address mezoAdmin;
        IUniversalRewardsDistributor urd;
        PermissionedMinter minter;
        string proofJson;
    }

    bytes32 DEFAULT_ADMIN_ROLE = 0x00;

    function run() external {
        _deploymsvUSD();
        // revert("Deployment complete");
    }

    function _run() internal {
        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));

        VaultData[] memory vaultsData = _setUp();

        vm.startBroadcast(deployerPk);
        for (uint256 i = 0; i < vaultsData.length; i++) {
            VaultData memory vault = vaultsData[i];
            vault.minter = _deploy(vault);
            vaultsData[i] = vault;
        }
        vm.stopBroadcast();


        for (uint256 i = 0; i < vaultsData.length; i++) {
            _mint(vaultsData[i]);
            _randomClaimTest(vaultsData[i]);
        }
        
        for (uint256 i = 0; i < vaultsData.length; i++) {
            console2.log(
                "Deployed URD at %s and PermissionedMinter at %s for vault %s",
                address(vaultsData[i].urd),
                address(vaultsData[i].minter),
                vaultsData[i].symbol
            );
        }

        revert("Deployment complete");
    }

    function _setUp() internal view returns (VaultData[] memory vaultsData) {
        /*
            URD mbhBTC   0xF73C154c1F57329A2c9C04C406470E8be14B0c3C
            URD mbhcbBTC 0x9F947e74b3DB089f480c7E2cA914777f6b07D286
            URD msvUSD   0x1A7ADC1d931D400B69f3CEd0C91B62d1af0B0712
        */
        vaultsData = new VaultData[](3);
        // mbhBTC mainnetVault = 0xa8A3De0c5594A09d0cD4C8abc4e3AaB9BaE03F36
        vaultsData[0].mezoVault = 0x807D4778abA870e4222904f5b528F68B350cE0E0;
        vaultsData[0].urd = IUniversalRewardsDistributor(0xF73C154c1F57329A2c9C04C406470E8be14B0c3C);
        vaultsData[0].symbol = "mbhBTC";

        // mbhcbBTC mainnetVault 0x63a76a4a94cAB1DD49fcf0d7E3FC53a78AC8Ec5C
        vaultsData[1].mezoVault = 0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8;
        vaultsData[1].urd = IUniversalRewardsDistributor(0x9F947e74b3DB089f480c7E2cA914777f6b07D286);
        vaultsData[1].symbol = "mbhcbBTC";

        // msvUSD mainnetVault 0x7207595E4c18a9A829B9dc868F11F3ADd8FCF626
        vaultsData[2].mezoVault = 0x07AFFA6754458f88db83A72859948d9b794E131b;
        vaultsData[2].urd = IUniversalRewardsDistributor(0x1A7ADC1d931D400B69f3CEd0C91B62d1af0B0712);
        vaultsData[2].symbol = "msvUSD";

        for (uint256 i = 0; i < vaultsData.length; i++) {
            vaultsData[i] = _fillVault(vaultsData[i]);
        }
    }

    function _fillVault(VaultData memory vaultData) internal view returns (VaultData memory) {
        Vault vault = Vault(payable(vaultData.mezoVault));
        address shareManagerAddr = address(IShareModule(vaultData.mezoVault).shareManager());
        vaultData.mezoAdmin = vault.getRoleMembers(DEFAULT_ADMIN_ROLE)[0];
        console2.log("Pushing vault with share manager at %s", shareManagerAddr);
        IERC20Metadata shareManager = IERC20Metadata(shareManagerAddr);
        require(IShareManager(shareManagerAddr).activeShares() == 0, "ACTIVE_SHARES_PRESENT");
        require(IShareManager(shareManagerAddr).totalShares() == 0, "TOTAL_SHARES_PRESENT");
        require(keccak256(abi.encodePacked(shareManager.symbol())) == keccak256(abi.encodePacked(vaultData.symbol)), "SYMBOL_MISMATCH");

        vaultData.proofJson = vm.readFile(_getProofPath(vaultData.symbol));

        return vaultData;
    }

    function _deploy(VaultData memory vaultData) internal returns (PermissionedMinter) {
        PermissionedMinter minter = new PermissionedMinter(
            Vault(payable(vaultData.mezoVault)),
            vaultData.mezoAdmin,
            address(vaultData.urd),
            uint224(vaultData.proofJson.readUint("$.totalShares")),
            3
        );
        return minter;
    }

    function _deploymsvUSD() internal {
        VaultData memory vaultData;
        // msvUSD mainnetVault 0x7207595E4c18a9A829B9dc868F11F3ADd8FCF626
        vaultData.mezoVault = 0x07AFFA6754458f88db83A72859948d9b794E131b;
        vaultData.urd = IUniversalRewardsDistributor(0x1A7ADC1d931D400B69f3CEd0C91B62d1af0B0712);
        vaultData.symbol = "msvUSD";

        _fillVault(vaultData);

        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));
        vm.startBroadcast(deployerPk);
        vaultData.minter = new PermissionedMinter(
            Vault(payable(vaultData.mezoVault)),
            vaultData.mezoAdmin,
            address(vaultData.urd),
            uint224(vaultData.proofJson.readUint("$.totalShares")),
            3
        );
        vm.stopBroadcast();
        console2.log("msvUSD Deployed PermissionedMinter at %s for vault %s", address(vaultData.minter), vaultData.symbol);

        //_mint(vaultData);
        //_randomClaimTest(vaultData);
    }

    function _mint(VaultData memory vaultData) internal {
        Vault vault = Vault(payable(vaultData.mezoVault));
        IShareManager shareManager = vault.shareManager();

        VaultState memory stateBefore = getVaultState(vault);

        vm.startPrank(vaultData.mezoAdmin);
        vault.grantRole(DEFAULT_ADMIN_ROLE, address(vaultData.minter));
        vaultData.minter.mint();
        vm.stopPrank();

        VaultState memory stateAfter = getVaultState(vault);

        compareVaultStates(stateBefore, stateAfter);
        uint256 mintedShares = shareManager.sharesOf(address(vaultData.urd));

        require(
            mintedShares == vaultData.proofJson.readUint("$.totalShares"),
            "SHARES_MINTED_MISMATCH"
        );

        for (uint256 roleIndex = 0; roleIndex < vault.supportedRoles(); roleIndex++) {
            bytes32 role = vault.supportedRoleAt(roleIndex);
            assertFalse(vault.hasRole(role, address(vaultData.minter)), "minter should not have any role");
        }

        string[] memory claimKeys = vm.parseJsonKeys(vaultData.proofJson, "$.claims");
        for (uint256 i = 0; i < claimKeys.length; i ++) {
            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            mintedShares -= vaultData.proofJson.readUint(string.concat(base, ".amount"));
        }
        require(mintedShares == 0, "TOTAL_SHARES_MISMATCH");
    }

    function _randomClaimTest(VaultData memory vaultData) internal {
        address rewardToken = address(IShareModule(vaultData.mezoVault).shareManager());

        vm.prank(vaultData.mezoAdmin);
        vaultData.urd.setRoot(vaultData.proofJson.readBytes32("$.root"), bytes32(0));
        string[] memory claimKeys = vm.parseJsonKeys(vaultData.proofJson, "$.claims");

        for (uint256 i = 0; i < claimKeys.length; i += 23) {
            address account = vm.parseAddress(claimKeys[i]);

            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            uint256 claimableTotal = vaultData.proofJson.readUint(string.concat(base, ".amount"));
            address recipient = vaultData.proofJson.readAddress(string.concat(base, ".recipient"));
            bytes32[] memory proof = vaultData.proofJson.readBytes32Array(string.concat(base, ".proof"));

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

    function _getProofPath(string memory symbol) internal pure returns (string memory) {
        return string(abi.encodePacked("data/", symbol, "/proofs.json"));
    }
}
