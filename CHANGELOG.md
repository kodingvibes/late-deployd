# Changelog

## [0.1.1](https://github.com/kodingvibes/late-deployd/compare/v0.1.0...v0.1.1) (2026-07-29)


### Bug Fixes

* make DB_PATH configurable via DEPOYD_DB_PATH env var ([40d6b0d](https://github.com/kodingvibes/late-deployd/commit/40d6b0d11f5336fe05507f40cab026127f2365e9))

## 0.1.0 (2026-07-27)


### Features

* initial extraction of late-deployd from late.kodingvibes.com ([e0fdaa9](https://github.com/kodingvibes/late-deployd/commit/e0fdaa9ca65fa8dd4c24c17965e45fb9dbfbc174))
* self-update script for late-deployd on its own deploy ([8ebc166](https://github.com/kodingvibes/late-deployd/commit/8ebc1665e78c6f96696ba54829c2c857c664ad8c))


### Bug Fixes

* **self-update:** detach the restart from parent cgroup ([28fdb63](https://github.com/kodingvibes/late-deployd/commit/28fdb63c50da2348770008d84779b686762486e5))
* **self-update:** minimize parent script lifetime ([bcf7618](https://github.com/kodingvibes/late-deployd/commit/bcf7618e3d2a335e31ab0ddff0635b9b0635f21a))
* **self-update:** shorter graceful shutdown, no healthcheck race ([fa0bb0d](https://github.com/kodingvibes/late-deployd/commit/fa0bb0dc88708e4c32cf7378261acca6ecc1fe88))
* **self-update:** truly detach via setsid+nohup helper ([8d760e9](https://github.com/kodingvibes/late-deployd/commit/8d760e973aa76f1740c31f0808d4a49b1d0b8248))


### Documentation

* note self-hosting contract in README ([16ac9c5](https://github.com/kodingvibes/late-deployd/commit/16ac9c5df995c9c27a69c75d44ec35ceb8007985))
