// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

// Mezo -> Ethereum return leg, step 1: deploy the two ethereum URDs, nothing else.
//
// One URD per payout token: mbhBTC holders claim tBTC, mbhcbBTC holders claim cbBTC.
// Each URD is owned by RETURN_URD_OWNER from the constructor, with no timelock and no root yet:
// fund it with what came back from mezo, build the proofs from its balance (npm run return:build),
// then the owner safe sets the root (ReturnDeploy._logSetRootCalldata) and ReturnDeploy.run() claims.
//
// Already deployed: RETURN_URD_MBHBTC / RETURN_URD_MBHCBBTC below, run() refuses to deploy again.
//
// Pinned to 0.8.19 because UniversalRewardsDistributor is pinned to 0.8.19.

import "forge-std/Script.sol";

import {UniversalRewardsDistributor} from "lib/morpho-urd/src/UniversalRewardsDistributor.sol";

// 3/5 safe with the same owners on mezo and ethereum
address constant RETURN_URD_OWNER = 0xd5aA2D083642e8Dec06a5e930144d0Af5a97496d;

// Deployed by run() from 0x4d551d74e851Bd93Ce44D5F588Ba14623249CDda at ethereum blocks 26226487 / 26226488.
address constant RETURN_URD_MBHBTC = 0xCf4cBB7994e75B7F8e7F7A52e880860bEaA7eE72;
address constant RETURN_URD_MBHCBBTC = 0x0746C70Ec99248Ccc1d6e29010e03C9fa5503653;

contract DeployReturnURDs is Script {
    function run() external {
        require(
            RETURN_URD_MBHBTC.code.length == 0 && RETURN_URD_MBHCBBTC.code.length == 0,
            "return URDs already deployed: RETURN_URD_MBHBTC / RETURN_URD_MBHCBBTC"
        );
        uint256 deployerPk = uint256(bytes32(vm.envBytes("HOT_DEPLOYER")));

        vm.startBroadcast(deployerPk);
        (UniversalRewardsDistributor mbhBTC, UniversalRewardsDistributor mbhcbBTC) = _deployURDs(RETURN_URD_OWNER);
        vm.stopBroadcast();

        console2.log("return URD mbhBTC   %s", address(mbhBTC));
        console2.log("return URD mbhcbBTC %s", address(mbhcbBTC));
    }

    function _deployURDs(address owner)
        internal
        returns (UniversalRewardsDistributor mbhBTC, UniversalRewardsDistributor mbhcbBTC)
    {
        require(owner != address(0), "OWNER not set");
        mbhBTC = new UniversalRewardsDistributor(owner, 0, bytes32(0), bytes32(0));
        mbhcbBTC = new UniversalRewardsDistributor(owner, 0, bytes32(0), bytes32(0));
        _checkDeployed(mbhBTC, owner);
        _checkDeployed(mbhcbBTC, owner);
    }

    function _checkDeployed(UniversalRewardsDistributor urd, address owner) internal view {
        require(urd.owner() == owner, "URD_OWNER_MISMATCH");
        require(urd.timelock() == 0, "URD_TIMELOCK_SET");
        require(urd.root() == bytes32(0), "URD_ROOT_SET");
    }
}
