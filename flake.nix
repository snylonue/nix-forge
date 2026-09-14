{
  description = "Forge server packaging for nix-minecraft";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nix-minecraft = {
      url = "github:Infinidoge/nix-minecraft";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self, nixpkgs, nix-minecraft }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      # Forge-only overlay, so this can be dropped into a nix-minecraft tree.
      # Mirrors the shape of nix-minecraft's own `pkgs/neoforge-servers`.
      overlays.forge-servers = final: prev: {
        forgeServers = import ./pkgs/forge-servers {
          lib = final.lib;
          callPackage = final.callPackage;
        };
      };

      # The scope is exposed as legacyPackages rather than packages: it is a
      # nested attrset, which `packages` does not permit.
      legacyPackages = forAll (pkgs: (pkgs.extend self.overlays.forge-servers).forgeServers);

      apps = forAll (pkgs: {
        update-forge = {
          type = "app";
          program = builtins.toString (pkgs.writeShellApplication {
            name = "update-forge";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              exec python3 ${./pkgs/forge-servers/update.py} "$@"
            '';
          });
        };
      });
    };
}
