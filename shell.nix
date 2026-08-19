{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  name = "breatheForecaster";

  buildInputs = with pkgs; [
    python3
    python3Packages.flake8
  ];

  shellHook = ''
    echo "Python shell loaded"
    echo "Run 'python forecaster.py --help' to test"
  '';
}
