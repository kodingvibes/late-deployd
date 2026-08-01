# Changelog

## [0.2.2](https://github.com/kodingvibes/late-deployd/compare/v0.2.1...v0.2.2) (2026-08-01)


### Bug Fixes

* **dashboard:** return deploys and streams as flat lists, move WS auth to subprotocol ([69b3dde](https://github.com/kodingvibes/late-deployd/commit/69b3dde220ab5a3985cd21fd3ca90baaf3557c6b))

## [0.2.1](https://github.com/kodingvibes/late-deployd/compare/v0.2.0...v0.2.1) (2026-08-01)


### Bug Fixes

* **history:** allow listeners and latency_ms as valid metrics ([543aaea](https://github.com/kodingvibes/late-deployd/commit/543aaea53c3c8f3bbe39cfe328304bb302c44fdc))
* **snapshot:** add missing icecast, deploys, db, streams gatherers ([1f9ea7e](https://github.com/kodingvibes/late-deployd/commit/1f9ea7ec59044e014136c5de781eb339cdbaae7d))
* **ws:** accept WebSocket before auth validation ([64d4b59](https://github.com/kodingvibes/late-deployd/commit/64d4b59b7c0f15ddc719cde1cd1d4ad49d1b015c))

## [0.2.0](https://github.com/kodingvibes/late-deployd/compare/v0.1.1...v0.2.0) (2026-07-31)


### Features

* **health:** expose poll_interval + manual tick via ?poll=1 ([827177e](https://github.com/kodingvibes/late-deployd/commit/827177e94abbf72ab4ace10a3a2f9bd763ecf98e))
* **health:** surface deploy state per repo and stuck-deployed detection ([1ac519d](https://github.com/kodingvibes/late-deployd/commit/1ac519d2b8597d3108c8929eee57cc8b14f0944f))
* poll origin every 10min and auto-deploy drifted repos ([85d30f0](https://github.com/kodingvibes/late-deployd/commit/85d30f0fa967c929a1d921fb6957a65a2fe002b8))

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
