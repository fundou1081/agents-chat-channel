# Conda 环境设置指南

## 创建 conda 环境

```bash
# 从 environment.yml 创建环境
conda env create -f environment.yml

# 激活环境
conda activate agents-chat-channel
```

## 安装项目

```bash
# 可编辑安装（开发模式）
pip install -e .

# 或者只安装依赖
pip install fastapi uvicorn[standard] pydantic aiosqlite rich
```

## 初始化数据目录

```bash
python -m agents_chat.v2.main init --data-dir ./data_v2
```

## 启动服务器

```bash
# 启动 API 服务器和 WebUI
python -m agents_chat.v2.server --port 8765 --data-dir ./data_v2
```

## 运行示例

```bash
# 被动模式示例
bash examples/e2e_bargain_real.sh

# 主动模式示例  
bash examples/e2e_autonomous.sh
```

## 运行测试

```bash
# 运行所有单元测试
pytest tests/unit/

# 运行集成测试
pytest tests/integration/
```

## 环境管理

```bash
# 查看当前环境
conda env list

# 导出当前环境
conda env export > environment.yml

# 删除环境
conda env remove -n agents-chat-channel
```

## 故障排除

如果遇到依赖冲突问题，可以尝试：

```bash
# 更新 conda
conda update conda

# 清理缓存
conda clean --all

# 重新创建环境
conda env remove -n agents-chat-channel
conda env create -f environment.yml
```
