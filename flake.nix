{
  description = "Koi: local Incus/ZFS compute and explicit guest image construction";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      control = import ./control-plane/package.nix { inherit pkgs; };
      kernel = import ./images/debian-runit/boot/q35-kernel.nix { inherit pkgs; };
      mkImage =
        args:
        import ./images/debian-runit/default.nix (
          {
            inherit pkgs kernel;
            koiPackage = control;
          }
          // args
        );
      image = mkImage { };
      package = import ./package.nix {
        inherit pkgs control;
        imageBuilder = image.builder;
      };
    in
    {
      lib = {
        inherit mkImage;
        bundleContract = 1;
        bootContract = "q35-runit-v3";
      };
      packages.${system} = {
        default = package;
        control-plane = control;
        q35-kernel = kernel;
        image-inputs = image.inputs;
        image-builder = image.builder;
      };
      nixosModules = {
        default = { ... }: {
          imports = [ ./integrations/nixos/module.nix ];
          services.koi.package = package;
        };
        control-plane = { ... }: {
          imports = [ ./control-plane/module.nix ];
          services.koi.package = control;
        };
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          package
          pkgs.python3
          pkgs.ruff
          pkgs.nixfmt
        ];
      };
      formatter.${system} = pkgs.nixfmt;
    };
}
