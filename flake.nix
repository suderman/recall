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
        # The dev shell runs checkout source; the package can lag uncommitted files.
        devRecall = pkgs.writeShellScriptBin "recall" ''
          if [ ! -d "''${PYTHONPATH%%:*}/recall" ]; then
            echo "recall: start the dev shell from the Recall checkout" >&2
            exit 1
          fi
          exec ${pythonEnv}/bin/python -m recall.cli.main "$@"
        '';
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
            devRecall
            pkgs.khal
            pkgs.notmuch
            pkgs.sqlite
            pkgs.tdlib
            pkgs.uv
          ];

          shellHook = ''
            # Only checkout source; inherited setup-hook paths include other Python versions.
            export PYTHONPATH="$PWD/src"
            export RECALL_ROOT="''${RECALL_ROOT:-''${XDG_DATA_HOME:-$HOME/.local/share}/recall/archive}"
            export TELEGRAM_TDLIB_LIBRARY_PATH="${pkgs.tdlib}/lib/libtdjson.so"
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.tdlib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
          '';
        };
      });
}
