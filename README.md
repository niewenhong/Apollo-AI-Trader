# Apollo Trader v3.8.1 补丁包

## 修改文件清单

| 文件 | 关键修改 |
|---|---|
| `main.py` | 启动时双市场流水线并行（ThreadPoolExecutor, max_workers=2） |
| `core/scheduler_jobs.py` | pipeline_job / optimization_job 双市场并行 |
| `core/strategy_engine.py` | boot() 接受 market 参数；同市场内同底层+同class去重；import copy 顶部 |
| `core/strategy_generator.py` | Regime 归一化表；同市场去重集合；pipeline 已存在跳过 |
| `core/strategy_lifecycle_manager.py` | on_strategy_deployed 改为 **kwargs 透传 |
| `core/regime_predictor.py` | 兼容 underlying_map 传入；新增 batch_compute 并行接口 |
| `core/risk_manager.py` | on_strategy_deployed 真实实现（登记+冲突检测+风控检查） |
| `ai/stock_selector.py` | 修复 typing 导入；新增 _dynamic_filter_hk() 港股动态筛选 |
| `ai/stock_diagnosis.py` | 兼容 KlineProvider 返回 dict/DataFrame 两种格式 |

## 使用方式

```bash
# 解压后覆盖到项目根目录
unzip -o apollo_v3.8.1.zip -d /path/to/apollo/

# 确认版本标记
grep "v3.8.1" main.py core/*.py ai/*.py
```

## 核心改动说明

### 1. 诊股/Regime/部署三阶段并行
- main.py: `_run_pipeline_for_market` 在两个市场间用 ThreadPoolExecutor 并行
- scheduler_jobs.py: pipeline_job 同样双市场并行
- 每个市场内部：诊股多线程(8 worker) → Regime多线程(8 worker) → 部署多线程(8 worker)

### 2. Pipeline 间去重
- strategy_generator.py: 同市场内 (base_symbol, class_name) 去重集合
- strategy_engine.py: boot(market=...) 按市场过滤；同市场内去重
- risk_manager.py: on_strategy_deployed 登记 + 冲突检测

### 3. Regime 归一化
- strategy_generator.py: REGIME_NORMALIZATION 表
  - range_mid/range_up/range_down → range
  - up_high → strong_bull
  - down_low → bear
  - 等

### 4. 港股动态选股
- stock_selector.py: _dynamic_filter_hk() 使用 Market.HK + HK.MAIN/HK.HSCEI

## 零功能回退确认
- 所有 v3.8.0 原有功能保留
- 新增并行/去重/归一化逻辑
- 不删除任何原有代码路径
