# 病历本 - 个人病历管理系统

手机拍照上传医院检查单，随时随地查看病历记录。

## Railway 部署步骤

### 1. 连接 GitHub 仓库
1. 打开 [railway.app](https://railway.app)，用 GitHub 账号登录
2. 点击 **New Project** → **Deploy from GitHub repo**
3. 选择 `medical-records` 仓库

### 2. 添加 PostgreSQL 数据库
1. 在项目面板中，点击 **+ New** → **Database** → **Add PostgreSQL**
2. Railway 会自动创建数据库并注入 `DATABASE_URL` 环境变量

### 3. 确认部署
- Railway 会自动检测 Python 项目并安装依赖
- 部署完成后，点击项目域名即可访问
- 如果没有自动生成域名，进入 **Settings** → **Networking** → **Generate Domain**

### 4. 手机使用
- 用手机浏览器打开 Railway 分配的域名
- 建议添加到手机主屏幕（Safari: 分享 → 添加到主屏幕）
- 点击「+ 上传」拍照或选择图片上传病历

## 功能说明
- **拍照上传**: 支持手机直接拍照或从相册选择
- **分类管理**: 血液检查、尿液检查、X光/CT/MRI、B超、心电图、处方/药单、诊断报告、其他
- **缩略图**: 自动生成缩略图，加速列表加载
- **全屏查看**: 点击记录可全屏查看原图
- **删除记录**: 在全屏查看时可删除记录

## 本地开发

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# 访问 http://localhost:8000
```
