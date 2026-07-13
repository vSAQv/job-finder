# devenv.nix
{pkgs, ...}: {
  packages = with pkgs; [
    python312
    python312Packages.playwright
  ];

  env = {
    PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
  };
}
