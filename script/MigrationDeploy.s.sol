// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import "forge-std/Script.sol";
import "forge-std/StdJson.sol";
import "forge-std/interfaces/IMulticall3.sol";

import {IShareManager} from "lib/flexible-vaults/src/interfaces/managers/IShareManager.sol";
import {IShareModule} from "lib/flexible-vaults/src/interfaces/modules/IShareModule.sol";
import {PermissionedMinter} from "lib/flexible-vaults/src/utils/PermissionedMinter.sol";

import {Vault} from "lib/flexible-vaults/src/vaults/Vault.sol";
import {Integration, VaultState} from "lib/flexible-vaults/test/PermissionedBuilderTest.s.sol";
import {IERC20} from "lib/openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "lib/openzeppelin-contracts/contracts/token/ERC20/extensions/IERC20Metadata.sol";

interface ISafe {
    function getTransactionHash(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address refundReceiver,
        uint256 _nonce
    ) external view returns (bytes32);

    function execTransaction(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address payable refundReceiver,
        bytes memory signatures
    ) external payable returns (bool success);

    function approveHash(bytes32 hashToApprove) external;
    function approvedHashes(address owner, bytes32 hash) external view returns (uint256);
    function nonce() external view returns (uint256);
    function getOwners() external view returns (address[] memory);
    function getThreshold() external view returns (uint256);
}

interface IMultiSendCallOnly {
    function multiSend(bytes memory transactions) external payable;
}

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
        //_testUnsignedSafeMultiSend();
        //_deploymbhBTC();
        //_acceptance_msvUSD();
        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));
        vm.startBroadcast(deployerPk);
        address deployer = vm.addr(deployerPk);
        //_claimForAccount_msvUSD(deployer);
        _claimBatch_msvUSD(0, 10);
        vm.stopBroadcast();
        revert("Deployment complete");
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
    

    function _deploymbhBTC() internal {
        VaultData memory vaultData;
        // mbhBTC mainnetVault = 0xa8A3De0c5594A09d0cD4C8abc4e3AaB9BaE03F36
        vaultData.mezoVault = 0x807D4778abA870e4222904f5b528F68B350cE0E0;
        vaultData.urd = IUniversalRewardsDistributor(0xF73C154c1F57329A2c9C04C406470E8be14B0c3C);
        vaultData.symbol = "mbhBTC";

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
        console2.log("mbhBTC Deployed PermissionedMinter at %s for vault %s", address(vaultData.minter), vaultData.symbol);

        //_mint(vaultData);
        //_randomClaimTest(vaultData);
    }

    function _deploymbhcbBTC() internal {
        VaultData memory vaultData;
        // mbhcbBTC mainnetVault 0x63a76a4a94cAB1DD49fcf0d7E3FC53a78AC8Ec5C
        vaultData.mezoVault = 0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8;
        vaultData.urd = IUniversalRewardsDistributor(0x9F947e74b3DB089f480c7E2cA914777f6b07D286);
        vaultData.symbol = "mbhcbBTC";

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
        console2.log("mbhcbBTC Deployed PermissionedMinter at %s for vault %s", address(vaultData.minter), vaultData.symbol);

        //_mint(vaultData);
        //_randomClaimTest(vaultData);
    }

    function _deploymsvUSD() internal {
        (VaultData memory vaultData,,) = _msvUSD_vaultData();
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

    function _acceptance_msvUSD() internal {
        (VaultData memory vaultData, address rewardToken, string[] memory claimKeys) = _msvUSD_vaultData();
        uint256 totalShares = vaultData.proofJson.readUint("$.totalShares");
        require(totalShares == IShareManager(rewardToken).activeShares(), "TOTAL_SHARES_MISMATCH");
        require(totalShares == IERC20(rewardToken).balanceOf(address(vaultData.urd)), "URD_SHARES_MISMATCH");
        uint256 totalRewarded;

        for (uint256 i = 0; i < claimKeys.length; i++) {
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
            totalRewarded += paid;
            assertEq(balAfter - balBefore, paid);
            assertEq(paid, claimableTotal); // because first claim; claimedTotal starts at 0
        }
        require(IERC20(rewardToken).balanceOf(address(vaultData.urd)) == 0, "URD_BALANCE_NOT_ZERO");
        require(totalRewarded == totalShares, "TOTAL_REWARDED_MISMATCH");
    }

    function _msvUSD_vaultData() internal view returns (VaultData memory vaultData, address rewardToken, string[] memory claimKeys) {
        vaultData.mezoVault = 0x07AFFA6754458f88db83A72859948d9b794E131b;
        vaultData.urd = IUniversalRewardsDistributor(0x1A7ADC1d931D400B69f3CEd0C91B62d1af0B0712);
        vaultData.symbol = "msvUSD";
        vaultData.proofJson = vm.readFile(_getProofPath(vaultData.symbol));
        rewardToken = address(IShareModule(vaultData.mezoVault).shareManager());
        claimKeys = vm.parseJsonKeys(vaultData.proofJson, "$.claims");
    }

    function _claimForAccount_msvUSD(address account) internal {
        (VaultData memory vaultData, address rewardToken, string[] memory claimKeys) = _msvUSD_vaultData();

        for (uint256 i = 0; i < claimKeys.length; i++) {
            if (vm.parseAddress(claimKeys[i]) != account) continue;

            string memory base = string.concat('$.claims["', claimKeys[i], '"]');
            uint256 claimable = vaultData.proofJson.readUint(string.concat(base, ".amount"));
            address recipient = vaultData.proofJson.readAddress(string.concat(base, ".recipient"));
            bytes32[] memory proof = vaultData.proofJson.readBytes32Array(string.concat(base, ".proof"));

            uint256 paid = vaultData.urd.claim(recipient, rewardToken, claimable, proof);
            console2.log("Claimed for account %s: %d", account, paid);
            return;
        }
        revert("account not found in msvUSD proofs");
    }

    function _claimBatch_msvUSD(uint256 startIdx, uint256 endIdx) internal {
        (VaultData memory vaultData, address rewardToken, string[] memory claimKeys) = _msvUSD_vaultData();
        require(endIdx <= claimKeys.length && startIdx < endIdx, "invalid range");

        IMulticall3 multicall = IMulticall3(0xcA11bde05977b3631167028862bE2a173976CA11);
        uint256 count = endIdx - startIdx;
        IMulticall3.Call3[] memory calls = new IMulticall3.Call3[](count);

        for (uint256 i = 0; i < count; i++) {
            string memory key = claimKeys[startIdx + i];
            string memory base = string.concat('$.claims["', key, '"]');
            uint256 claimable = vaultData.proofJson.readUint(string.concat(base, ".amount"));
            address recipient = vaultData.proofJson.readAddress(string.concat(base, ".recipient"));
            bytes32[] memory proof = vaultData.proofJson.readBytes32Array(string.concat(base, ".proof"));

            calls[i] = IMulticall3.Call3({
                target: address(vaultData.urd),
                allowFailure: false,
                callData: abi.encodeCall(IUniversalRewardsDistributor.claim, (recipient, rewardToken, claimable, proof))
            });
        }

        IMulticall3.Result[] memory results = multicall.aggregate3(calls);
        //for (uint256 i = 0; i < results.length; i++) {
        //    uint256 paid = abi.decode(results[i].returnData, (uint256));
        //    console2.log("Claimed for account %s: %d", vm.parseAddress(claimKeys[startIdx + i]), paid);
        //}
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

    /// @notice Simulates the exact Safe tx from the UI by pranking threshold owners to
    ///         approveHash, then calling execTransaction with approved-hash signatures.
    ///         Fields match what you paste from the Safe UI.
    function _testUnsignedSafeMultiSend() internal {
        ISafe safe = ISafe(0xd5aA2D083642e8Dec06a5e930144d0Af5a97496d);

        // --- Paste your Safe UI fields here ---
        address to = 0x9641d764fc13c8B624c04430C7356C1C7C8102e2;
        uint256 value = 0;
        bytes memory data = hex"8d80ff0a0000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000022400807d4778aba870e4222904f5b528f68b350ce0e0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000442f2ff15d00000000000000000000000000000000000000000000000000000000000000000000000000000000000000002adf0a3234cc9ae4e184c7d379f04a53ae77e0e9002adf0a3234cc9ae4e184c7d379f04a53ae77e0e9000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000041249c58b00807d4778aba870e4222904f5b528f68b350ce0e000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000044d547741f00000000000000000000000000000000000000000000000000000000000000000000000000000000000000002adf0a3234cc9ae4e184c7d379f04a53ae77e0e900f73c154c1f57329a2c9c04c406470e8be14b0c3c0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004442af83fb09d2f2dc80cf09bc6f2796f121451c0a5879665633ab91c565a357412737c12a000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
        uint8 operation = 1;
        uint256 safeTxNonce = 8;

        bytes32 txHash = safe.getTransactionHash(
            to, value, data, operation,
            0, 0, 0, address(0), payable(address(0)),
            safeTxNonce
        );

        // Prank threshold owners to approve the hash (no real signatures needed in simulation)
        address[] memory owners = safe.getOwners();
        uint256 threshold = safe.getThreshold();
        for (uint256 i = 0; i < threshold; i++) {
            vm.prank(owners[i]);
            safe.approveHash(txHash);
        }

        // Safe requires signatures sorted by signer address ascending — sort the first `threshold` owners
        address[] memory signers = new address[](threshold);
        for (uint256 i = 0; i < threshold; i++) signers[i] = owners[i];
        for (uint256 i = 0; i < threshold - 1; i++) {
            for (uint256 j = i + 1; j < threshold; j++) {
                if (signers[i] > signers[j]) (signers[i], signers[j]) = (signers[j], signers[i]);
            }
        }

        // Build signatures: approved-hash format (v=1): abi.encodePacked(bytes32(owner), bytes32(0), uint8(1))
        bytes memory signatures;
        for (uint256 i = 0; i < threshold; i++) {
            signatures = abi.encodePacked(
                signatures,
                bytes32(uint256(uint160(signers[i]))), // r = owner address
                bytes32(0),                            // s = 0
                uint8(1)                               // v = 1 means approvedHash
            );
        }

        bool success = safe.execTransaction(
            to, value, data, operation,
            0, 0, 0, address(0), payable(address(0)),
            signatures
        );
        require(success, "Safe execTransaction failed");
    }
}
