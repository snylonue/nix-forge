{
  description = "Forge server packaging for nix-minecraft";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };

  outputs =
    { self, nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      # `allowUnfree` because the Forge derivations are
      # `unfreeRedistributable`. This affects only the flake's own
      # `legacyPackages` and `apps`; the overlay is applied to the consumer's
      # nixpkgs, so `nixpkgs.config.allowUnfree` governs there instead.
      forEachSystem =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f (
            import nixpkgs {
              inherit system;
              config.allowUnfree = true;
            }
          )
        );
    in
    {
      # Forge-only overlay, so this can be dropped into a nix-minecraft tree.
      # Mirrors the shape of nix-minecraft's own `pkgs/neoforge-servers`.
      overlays.forge-servers = final: prev: {
        forgeServers = import ./pkgs/forge-servers {
          inherit (final) lib callPackage;
        };
      };

      # The scope is exposed as legacyPackages rather than packages: it is a
      # nested attrset, which `packages` does not permit.
      legacyPackages = forEachSystem (pkgs: (pkgs.extend self.overlays.forge-servers).forgeServers);

      apps = forEachSystem (pkgs: {
        update-forge = {
          type = "app";
          program = pkgs.lib.getExe (
            pkgs.writeShellApplication {
              name = "update-forge";
              runtimeInputs = [ pkgs.python3 ];
              text = ''
                exec python3 ${./pkgs/forge-servers/update.py} "$@"
              '';
            }
          );
        };
      });
    };
}
