{
  description = "Koi: persistent per-project VMs on a NixOS host";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  inputs.guest-nixpkgs.url = "github:NixOS/nixpkgs/e7439b6b14ad3cc35d05608ebca9bce01a25f5f8";
  outputs =
    {
      self,
      nixpkgs,
      guest-nixpkgs,
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      guest = import guest-nixpkgs {
        inherit system;
        config.allowUnfreePredicate = package: pkgs.lib.getName package == "claude-code";
      };
      plugins = import ./adapters { pkgs = guest; };
      koi = import ./koi { inherit pkgs plugins; };
      template = import ./images/nix-runit {
        inherit pkgs guest plugins;
        guestNixpkgs = guest-nixpkgs;
      };
    in
    {
      packages.${system} = {
        koi-inputs = template.inputs;
        koi-builder = template.builder;
        image = template.image;
        koi = koi.cli;
        koi-tools = koi.tools;
        koi-cli = koi.cli;
        koi-guest-tools = template.guestTools;
        default = koi.cli;
      };
      nixosModules.default = { ... }: {
        imports = [ ./nixos/module.nix ];
        services.koi.package = nixpkgs.lib.mkDefault koi.cli;
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.python3
          pkgs.ruff
          pkgs.nixfmt
        ];
      };
      formatter.${system} = pkgs.nixfmt;
    };
}
