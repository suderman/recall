{
  description = "Recall Python development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        pythonPackages = pkgs.python312Packages;
        runtimeDeps = with pythonPackages; [
          fastapi
          httpx
          python-dotenv
          sqlalchemy
          typer
          uvicorn
        ];
        devDeps = with pythonPackages; [
          pytest
          ruff
        ];
        pythonEnv = python.withPackages (_: runtimeDeps ++ devDeps);
      in {
        packages.default = pythonPackages.buildPythonApplication {
          pname = "recall";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          build-system = with pythonPackages; [
            setuptools
            wheel
          ];
          dependencies = runtimeDeps;
          postFixup = ''
            wrapProgram "$out/bin/recall" \
              --set-default TELEGRAM_TDLIB_LIBRARY_PATH "${pkgs.tdlib}/lib/libtdjson.so" \
              --prefix LD_LIBRARY_PATH : "${pkgs.lib.makeLibraryPath [ pkgs.tdlib ]}"
          '';
          meta = {
            description = "Recall command-line interface";
            mainProgram = "recall";
          };
        };

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/recall";
          meta = self.packages.${system}.default.meta;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            self.packages.${system}.default
            pkgs.notmuch
            pkgs.sqlite
            pkgs.tdlib
            pkgs.uv
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
            export RECALL_ROOT="$PWD"
            export TELEGRAM_TDLIB_LIBRARY_PATH="${pkgs.tdlib}/lib/libtdjson.so"
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.tdlib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
          '';
        };
      });
}
