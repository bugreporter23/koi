{ pkgs }:
{
  systemPackages = [
    pkgs.util-linux
    pkgs.socat
  ];
  services.inference = ./guest-forwarder.sh;
}
