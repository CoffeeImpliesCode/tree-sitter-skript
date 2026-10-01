{ pkgs, lib, config, inputs, ... }:

rec {
  packages = with pkgs; [
    git
    jujutsu
    nodejs
    gcc
    tree-sitter
    # lldb
    # llvm
    # lld
    # clang
    # glibc
    # raylib
    # dyncall
    # gcc
  ];

  env = {
    LD_LIBRARY_PATH = "${pkgs.lib.makeLibraryPath packages}";
    INCLUDE_DIRECTORIES = "${pkgs.lib.makeIncludePath packages}";
  };

  enterShell = ''
  '';
}
