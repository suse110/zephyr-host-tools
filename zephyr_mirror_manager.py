#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zephyr 本地镜像管理工具
支持功能：
1. init: 遍历目录生成 Git 仓库镜像（.git 裸仓库）
   - 如果指定了 --west-yml 参数，则从 west.yml 文件解析项目信息
   - 如果未指定 --west-yml 参数，则从当前目录递归搜索 .git 仓库
2. sync: 同步已生成的镜像到远程最新版本
注：脚本必须在 Zephyr 项目根目录执行（包含 .west/ 和 zephyr/ 目录）
"""

import os
import sys
import logging
import shutil
import subprocess
import argparse
from pathlib import Path
from typing import List, Optional, Tuple, Set, Dict
import yaml  # 需新增依赖：pip install pyyaml

# ===================== 全局配置（按需修改）=====================
# 默认镜像根目录
DEFAULT_MIRROR_ROOT: str = r"F:\workspace\src\zephyr-src\zephyr-mirror"
# 默认跳过的目录/仓库
DEFAULT_SKIP_DIRS: Set[str] = {".git", ".west", "__pycache__", "node_modules", "build"}
DEFAULT_SKIP_REPOS: Set[str] = set()
# 日志配置
DEFAULT_LOG_LEVEL: str = "INFO"
DEFAULT_LOG_FILE: str = "zephyr_mirror_manager.log"
# ===================== 全局配置结束 =====================


def setup_logger(log_level: str, log_file: str) -> logging.Logger:
    """
    配置日志系统：控制台+文件输出
    :param log_level: 日志级别（DEBUG/INFO/WARN/ERROR/CRITICAL）
    :param log_file: 日志文件路径
    :return: 配置好的 logger 实例
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    logger = logging.getLogger("zephyr_mirror_manager")
    logger.setLevel(level_map.get(log_level.upper(), logging.INFO))
    logger.handlers.clear()

    # 控制台处理器（简洁格式）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level_map.get(log_level.upper(), logging.INFO))
    console_formatter = logging.Formatter("%(levelname)s: %(message)s")
    console_handler.setFormatter(console_formatter)

    # 文件处理器（详细格式）
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # 文件始终输出 DEBUG 级别
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def parse_west_yml(west_yml_path: Path, logger: logging.Logger) -> List[dict]:
    """
    解析 Zephyr 官方 west.yml（兼容嵌套 import、隐式 remote、name-allowlist）
    :param west_yml_path: west.yml 文件路径
    :param logger: 日志实例
    :return: 仓库信息列表，格式：[{"name": 仓库名, "remote": remote别名, "url": 完整Git地址}]
    """
    # 1. 基础校验
    if not west_yml_path.exists() or not west_yml_path.is_file():
        logger.error(f"west.yml 文件不存在：{west_yml_path.absolute()}")
        return []

    # 2. 读取并解析 west.yml
    try:
        with open(west_yml_path, "r", encoding="utf-8") as f:
            west_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"解析 west.yml 失败（YAML 格式错误）：{str(e)}")
        return []

    # 3. 提取 remotes 映射表 + 默认 remote
    manifest = west_config.get("manifest", {})
    remotes = {}
    default_remote = manifest.get("default-remote")  # 全局默认 remote
    for remote in manifest.get("remotes", []):
        remote_name = remote.get("name")
        remote_url_base = remote.get("url-base")
        if remote_name and remote_url_base:
            remotes[remote_name] = remote_url_base
    logger.debug(f"解析到 remotes：{remotes} | 默认 remote：{default_remote}")

    # 4. 递归解析所有项目（处理嵌套 import）
    all_projects = []
    processed_names = set()  # 避免重复解析

    def _recursive_parse_projects(projects: List[dict], parent_remote: Optional[str] = None):
        """递归解析项目（处理 import 嵌套）"""
        for proj in projects:
            # 跳过空项目/已处理项目
            if not isinstance(proj, dict) or proj.get("name") in processed_names:
                continue

            proj_name = proj.get("name")
            if not proj_name:
                logger.warning(f"跳过无效项目：缺少 name - {proj}")
                continue

            # 确定项目的 remote（优先级：项目自身 > 父项目 > 全局默认）
            proj_remote = proj.get("remote", parent_remote or default_remote)
            if not proj_remote:
                logger.warning(f"跳过项目 {proj_name}：无可用 remote（自身/父/默认均无）")
                continue
            if proj_remote not in remotes:
                logger.warning(f"跳过项目 {proj_name}：remote {proj_remote} 未定义")
                continue

            # 拼接完整 Git 地址（兼容 repo-path 字段）
            repo_path = proj.get("repo-path", proj_name)
            url_base = remotes[proj_remote]
            proj_url = f"{url_base.rstrip('/')}/{repo_path}.git"

            # 记录项目信息
            all_projects.append({
                "name": proj_name,
                "remote": proj_remote,
                "url": proj_url
            })
            processed_names.add(proj_name)
            logger.debug(f"解析项目：{proj_name} | remote：{proj_remote} | URL：{proj_url}")

            # 递归解析 import 中的嵌套项目（继承父项目的 remote）
            import_config = proj.get("import")
            if import_config:
                # 处理 import 中的 name-allowlist（仅解析指定的子项目）
                allowlist = import_config.get("name-allowlist", [])
                if allowlist and isinstance(allowlist, list):
                    # 嵌套项目的配置在 zephyr/west.yml 的子目录中，需拼接路径
                    zephyr_path = Path(west_yml_path).parent / proj.get("path", "zephyr")
                    sub_west_yml = zephyr_path / "west.yml"
                    if sub_west_yml.exists():
                        try:
                            with open(sub_west_yml, "r", encoding="utf-8") as f:
                                sub_config = yaml.safe_load(f)
                            sub_projects = sub_config.get("manifest", {}).get("projects", [])
                            # 仅解析 allowlist 中的项目
                            filtered_sub_projs = [p for p in sub_projects if p.get("name") in allowlist]
                            _recursive_parse_projects(filtered_sub_projs, parent_remote=proj_remote)
                        except Exception as e:
                            logger.warning(f"解析子 west.yml 失败：{sub_west_yml}，错误：{str(e)}")

    # 启动递归解析
    _recursive_parse_projects(manifest.get("projects", []))

    # 5. 输出最终解析结果
    logger.info(f"从 west.yml 解析到 {len(all_projects)} 个有效仓库")
    for p in all_projects:
        logger.info(f"仓库：{p['name']} | Remote：{p['remote']} | URL：{p['url']}")

    return all_projects


def check_git_env() -> bool:
    """
    检查 Git 环境是否可用
    :return: Git 环境是否可用
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    try:
        subprocess.run(
            ["git", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True
        )
        logger.info("Git 环境检查通过")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Git 环境检查失败：{str(e)}")
        logger.error("请安装 Git 并添加到系统环境变量 PATH")
        return False


def execute_git_command(
    cmd: List[str],
    cwd: Optional[Path] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    通用 Git 命令执行函数
    :param cmd: Git 命令列表
    :param cwd: 执行目录
    :return: (是否成功, 标准输出, 标准错误)
    """
    logger = logging.getLogger("zephyr_mirror_manager")

    try:
        logger.debug(f"执行 Git 命令：{' '.join(cmd)} (工作目录：{cwd or '当前目录'})")
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            cwd=str(cwd) if cwd else None
        )
        return True, result.stdout.strip(), result.stderr.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stdout.strip(), e.stderr.strip()
    except Exception as e:
        logger.error(f"执行 Git 命令时发生未知错误：{str(e)}")
        return False, None, str(e)


# ------------------------------ Init 功能：生成镜像 ------------------------------
def find_git_repos(
    start_dir: Path,
    skip_dirs: Set[str],
    logger: logging.Logger
) -> List[Path]:
    """
    遍历目录找到所有 Git 仓库（找到 .git 后跳过子目录）
    :param start_dir: 起始遍历目录
    :param skip_dirs: 跳过的目录名
    :return: Git 仓库路径列表
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    git_repos: List[Path] = []

    if not start_dir.exists() or not start_dir.is_dir():
        logger.warning(f"遍历目录不存在：{start_dir.absolute()}")
        return git_repos

    for item in start_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name in skip_dirs:
            logger.debug(f"跳过目录：{item.absolute()}")
            continue

        # 检查是否是 Git 仓库
        git_dir = item / ".git"
        if git_dir.exists() and git_dir.is_dir():
            repo_path = item.absolute()
            git_repos.append(repo_path)
            logger.info(f"找到 Git 仓库：{repo_path}")
            continue

        # 递归遍历子目录
        sub_repos = find_git_repos(item, skip_dirs)
        git_repos.extend(sub_repos)

    return git_repos


def mirror_single_repo(
    repo_path: Path,
    mirror_repos_dir: Path,
    logger: logging.Logger
) -> bool:
    """
    生成单个仓库的镜像（裸仓库）
    :param repo_path: 源仓库路径
    :param mirror_repos_dir: 镜像输出目录
    :return: 是否成功
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    repo_name = repo_path.name
    # 判断路径/名称是否含 "hal"，添加前缀
    if "hal" in str(repo_path).lower():
        repo_name = f"hal_{repo_name}"
    bare_repo_path = mirror_repos_dir / f"{repo_name}.git"
    logger.info(f"开始制作镜像 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
    # 执行镜像命令
    cmd = ["git", "clone", "--mirror", str(repo_path), str(bare_repo_path)]
    success, stdout, stderr = execute_git_command(cmd)

    if success:
        if stdout:
            logger.debug(f"{repo_name} 镜像日志：{stdout}")
        logger.info(f"✅ 镜像制作成功 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
        return True
    else:
        logger.error(f"❌ 镜像制作失败 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
        if stderr:
            logger.error(f"错误信息 | 仓库：{repo_name} | 详情：{stderr}")
        return False


def ensure_dir_exists(dir_path: Path) -> bool:
    """
    确保目录存在，不存在则创建；创建失败则记录日志并返回 False
    :param dir_path: 要检查/创建的目录路径
    :return: 是否成功（目录存在/创建成功返回 True，否则 False）
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    try:
        if not dir_path.exists():
            logger.info(f"目录不存在，自动创建：{dir_path.absolute()}")
            dir_path.mkdir(parents=True, exist_ok=True)  # parents=True 自动创建父目录
        return True
    except PermissionError:
        logger.error(f"权限不足，无法创建目录：{dir_path.absolute()}")
        logger.error("请以管理员/root 身份运行脚本，或检查目录权限")
        return False
    except Exception as e:
        logger.error(f"创建目录失败：{dir_path.absolute()}，错误：{str(e)}")
        return False


def parse_west_yml_for_local(west_yml_path: Path) -> List[dict]:
    """
    解析 Zephyr 官方 west.yml 获取本地仓库路径和镜像名
    :param west_yml_path: west.yml 文件路径
    :return: 仓库信息列表，格式：[{"name": 镜像仓库名, "path": 本地仓库路径}]
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    # 1. 基础校验
    if not west_yml_path.exists() or not west_yml_path.is_file():
        logger.error(f"west.yml 文件不存在：{west_yml_path.absolute()}")
        return []

    # 2. 读取并解析 west.yml
    try:
        with open(west_yml_path, "r", encoding="utf-8") as f:
            west_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"解析 west.yml 失败（YAML 格式错误）：{str(e)}")
        return []

    # 3. 提取项目信息
    manifest = west_config.get("manifest", {})
    projects = []
    
    for proj in manifest.get("projects", []):
        if not isinstance(proj, dict):
            continue
            
        proj_name = proj.get("name")
        if not proj_name:
            logger.warning(f"跳过无效项目：缺少 name - {proj}")
            continue
            
        # 使用path字段，如果不存在则使用name作为path
        proj_path = proj.get("path", proj_name)
        
        projects.append({
            "name": proj_name,
            "path": proj_path
        })
        logger.debug(f"解析项目：{proj_name:<20} | 路径：{proj_path}")

    logger.info(f"从 west.yml 解析到 {len(projects)} 个项目")
    for p in projects:
        logger.info(f"项目：{p['name']:<20} | 路径：{p['path']}")

    return projects


def mirror_single_repo_by_name(
    repo_path: Path,
    mirror_repos_dir: Path,
    repo_name: str,
) -> bool:
    """
    生成单个仓库的镜像（裸仓库），使用指定的仓库名
    :param repo_path: 源仓库路径
    :param mirror_repos_dir: 镜像输出目录
    :param repo_name: 镜像仓库名
    :return: 是否成功
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    bare_repo_path = mirror_repos_dir / f"{repo_name}.git"
    logger.info(f"开始制作镜像 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
    
    # 如果目标已经存在，先删除
    if bare_repo_path.exists():
        logger.info(f"镜像已存在，删除后重新创建：{bare_repo_path.absolute()}")
        shutil.rmtree(bare_repo_path, ignore_errors=True)
    
    # 执行镜像命令
    cmd = ["git", "clone", "--mirror", str(repo_path), str(bare_repo_path)]
    success, stdout, stderr = execute_git_command(cmd)

    if success:
        if stdout:
            logger.debug(f"{repo_name} 镜像日志：{stdout}")
        logger.info(f"✅ 镜像制作成功 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
        return True
    else:
        logger.error(f"❌ 镜像制作失败 | 源路径：{repo_path.absolute()} | 目的路径：{bare_repo_path.absolute()}")
        if stderr:
            logger.error(f"错误信息 | 仓库：{repo_name} | 详情：{stderr}")
        return False


def is_zephyr_root_directory() -> bool:
    """
    检查当前目录是否为Zephyr项目根目录
    :return: 如果是Zephyr根目录返回True，否则返回False
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    # Zephyr项目根目录应该包含.west目录和zephyr目录
    current_dir = Path.cwd()
    logger.debug(f"检查当前目录是否为Zephyr根目录：{current_dir.absolute()}")
    
    # Zephyr项目根目录应该包含.west目录和zephyr目录
    west_dir_exists = (current_dir / ".west").is_dir()
    zephyr_dir_exists = (current_dir / "zephyr").is_dir()
    
    if west_dir_exists and zephyr_dir_exists:
        logger.info(f"确认当前目录为Zephyr项目根目录：{current_dir.absolute()}")
        return True
    else:
        logger.error(f"当前目录不是Zephyr项目根目录：{current_dir.absolute()}")
        if not west_dir_exists:
            logger.error("缺少 '.west' 目录")
        if not zephyr_dir_exists:
            logger.error("缺少 'zephyr/' 目录")
        return False


def init_mirror(
    mirror_root: str,
    west_yml_path: Optional[str],  # west.yml 解析模式的文件路径（如果为None则是目录扫描模式）
    clean_old: bool,
    skip_dirs: Set[str],
) -> None:
    """
    初始化 Zephyr 镜像（支持两种模式：目录扫描 / west.yml 解析）
    :param mirror_root: 镜像根目录
    :param west_yml_path: west.yml 解析模式的文件路径（如果为None则是目录扫描模式）
    :param clean_old: 是否清理旧镜像
    :param skip_dirs: 目录扫描模式跳过的目录
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    logger.info("=" * 60)
    
    # 检查是否在Zephyr项目根目录执行脚本
    if not is_zephyr_root_directory():
        logger.error("错误：脚本必须在Zephyr项目根目录执行！")
        sys.exit(1)
    
    mirror_root_path = Path(mirror_root)
    mirror_repos_dir = mirror_root_path / "repos"
    success_count = 0  # 初始化成功计数器，避免未定义

    # 模式2：west.yml 解析模式
    if west_yml_path:
        logger.info("         初始化 Zephyr 本地镜像（west.yml 解析模式）")
        logger.info(f"west.yml 文件路径：{Path(west_yml_path).absolute()}")
        logger.info(f"镜像输出目录：{mirror_repos_dir.absolute()}")

        # 检查 Git 环境
        if not check_git_env():
            sys.exit(1)

        # 清理旧镜像
        if clean_old and mirror_repos_dir.exists():
            logger.warning("清理旧镜像目录...")
            try:
                shutil.rmtree(mirror_repos_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"清理旧镜像失败：{str(e)}")
                sys.exit(1)

        # 确保目录存在
        if not ensure_dir_exists(mirror_root_path):
            sys.exit(1)
        if not ensure_dir_exists(mirror_repos_dir):
            sys.exit(1)

        # 解析 west.yml 获取仓库列表
        west_yml = Path(west_yml_path).absolute()
        proj_list = parse_west_yml_for_local(west_yml)
        if not proj_list:
            logger.error("未从 west.yml 解析到任何仓库，终止镜像")
            sys.exit(1)

        # 批量处理仓库：根据path查找本地仓库 → 制作镜像
        success_count = 0
        for proj in proj_list:
            proj_name = proj["name"]
            proj_path = proj["path"]
            
            # 查找本地仓库
            local_repo_path = Path(proj_path)
            if not local_repo_path.exists() or not (local_repo_path / ".git").exists():
                logger.warning(f"本地仓库不存在或不是git仓库：{local_repo_path.absolute()}")
                continue

            # 制作镜像，使用name作为镜像仓库名
            if mirror_single_repo_by_name(local_repo_path, mirror_repos_dir, proj_name):
                success_count += 1
    else:
        # 模式1：目录扫描模式，从当前目录开始搜索
        logger.info("         初始化 Zephyr 本地镜像（目录扫描模式）")
        start_dir_path = Path.cwd().absolute()  # 当前工作目录
        
        logger.info(f"遍历起始目录：{start_dir_path.absolute()}")
        logger.info(f"镜像输出目录：{mirror_repos_dir.absolute()}")

        # 检查 Git 环境
        if not check_git_env():
            sys.exit(1)

        # 清理旧镜像
        if clean_old and mirror_repos_dir.exists():
            logger.warning("清理旧镜像目录...")
            try:
                shutil.rmtree(mirror_repos_dir, ignore_errors=False)
            except Exception as e:
                logger.error(f"清理旧镜像失败：{str(e)}")
                sys.exit(1)

        # 确保目录存在
        if not ensure_dir_exists(mirror_root_path):
            sys.exit(1)
        if not ensure_dir_exists(mirror_repos_dir):
            sys.exit(1)

        # 查找 Git 仓库
        logger.info("开始遍历目录，识别 Git 仓库...")
        git_repos = find_git_repos(start_dir_path, skip_dirs)

        if not git_repos:
            logger.error("未找到任何 Git 仓库！")
            sys.exit(1)

        logger.info(f"共找到 {len(git_repos)} 个 Git 仓库待镜像")

        # 批量生成镜像
        logger.info("开始批量生成镜像...")
        success_count = 0
        for repo in git_repos:
            if mirror_single_repo(repo, mirror_repos_dir):
                success_count += 1
            
    # 输出结果
    logger.info("=" * 60)
    logger.info("镜像初始化完成！")
    # 安全获取待处理仓库数量
    total_count = len(git_repos) if ('git_repos' in locals()) else len(proj_list) if (west_yml_path and 'proj_list' in locals()) else 0
    logger.info(f"成功：{success_count} 个 | 失败：{total_count - success_count} 个")
    logger.info(f"镜像仓库存放目录：{mirror_repos_dir.absolute()}")
    logger.info("=" * 60)


# ------------------------------ Sync 功能：同步镜像 ------------------------------
def find_bare_repos(
    mirror_repos_dir: Path,
    skip_repos: Set[str],
) -> List[Path]:
    """
    查找镜像目录下的所有裸仓库
    :param mirror_repos_dir: 镜像目录
    :param skip_repos: 跳过的仓库名
    :return: 裸仓库路径列表
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    bare_repos: List[Path] = []

    if not mirror_repos_dir.exists() or not mirror_repos_dir.is_dir():
        logger.error(f"镜像目录不存在：{mirror_repos_dir.absolute()}")
        return bare_repos

    for item in mirror_repos_dir.iterdir():
        if not item.is_dir() or not item.name.endswith(".git"):
            continue
        if item.name in skip_repos:
            logger.debug(f"跳过仓库：{item.name}")
            continue

        bare_repos.append(item)
        logger.debug(f"找到裸仓库：{item.name}")

    return bare_repos


def sync_single_repo(
    repo_path: Path,
) -> bool:
    """
    同步单个裸仓库到远程最新版本
    :param repo_path: 裸仓库路径
    :return: 是否成功
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    repo_name = repo_path.name

    # 获取远程仓库地址（作为同步的目的路径）
    cmd_remote = ["git", "remote", "get-url", "origin"]
    success_remote, remote_url, _ = execute_git_command(cmd_remote, cwd=repo_path)
    remote_url = remote_url if success_remote else "未知远程地址"

    # 同步日志输出本地镜像路径（源）和远程地址（目的）
    logger.info(f"开始同步镜像 | 本地镜像路径：{repo_path.absolute()} | 远程仓库：{remote_url}")
    
    # 验证是否是裸仓库
    cmd_check = ["git", "rev-parse", "--is-bare-repository"]
    success, stdout, _ = execute_git_command(cmd_check, cwd=repo_path)
    if not success or stdout != "true":
        logger.error(f"❌ {repo_name} 不是合法的裸仓库")
        return False

    # 执行同步
    cmd_sync = ["git", "remote", "update"]
    success, stdout, stderr = execute_git_command(cmd_sync, cwd=repo_path)

    if success:
        if stdout:
            logger.info(f"{repo_name} 同步日志：{stdout}")
        else:
            logger.info(f"✅ {repo_name} 已同步到最新（无变更）")
        return True
    else:
        logger.error(f"❌ {repo_name} 同步失败")
        if stderr:
            logger.error(f"错误信息：{stderr}")
        return False


def sync_mirror(
    mirror_root: str,
    skip_repos: Set[str],
) -> None:
    """
    同步 Zephyr 镜像到远程最新版本
    :param mirror_root: 镜像根目录
    :param skip_repos: 跳过的仓库
    """
    logger = logging.getLogger("zephyr_mirror_manager")
    
    logger.info("=" * 60)
    logger.info("         同步 Zephyr 本地镜像到远程最新版本")
    logger.info("=" * 60)

    # 初始化路径
    mirror_root_path = Path(mirror_root)
    mirror_repos_dir = mirror_root_path / "repos"
    
    # 检查镜像根目录是否存在
    if not ensure_dir_exists(mirror_root_path):
        sys.exit(1)
    if not ensure_dir_exists(mirror_repos_dir):
        sys.exit(1)

    logger.info(f"镜像仓库存放目录：{mirror_repos_dir.absolute()}")

    # 检查 Git 环境
    if not check_git_env():
        sys.exit(1)

    # 查找裸仓库
    logger.info("开始查找待同步的裸仓库...")
    bare_repos = find_bare_repos(mirror_repos_dir, skip_repos)

    if not bare_repos:
        logger.error("未找到任何需要同步的裸仓库！")
        sys.exit(1)

    logger.info(f"共找到 {len(bare_repos)} 个裸仓库待同步")

    # 批量同步
    logger.info("开始批量同步...")
    success_count = 0
    for repo in bare_repos:
        if sync_single_repo(repo):
            success_count += 1

    # 输出结果
    logger.info("=" * 60)
    logger.info("镜像同步完成！")
    logger.info(f"成功：{success_count} 个 | 失败：{len(bare_repos) - success_count} 个")
    logger.info("=" * 60)


# ------------------------------ 主函数：参数解析 + 功能调度 ------------------------------
def main() -> None:
    """主函数：解析命令行参数，调度对应功能"""
    # 1. 构建命令行参数解析器
    parser = argparse.ArgumentParser(
        description="Zephyr 本地镜像管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  1. 初始化镜像（目录扫描模式，在当前目录递归搜索 .git 仓库）：
     python zephyr_mirror_manager.py init
  2. 初始化镜像（west.yml 解析模式）：
     python zephyr_mirror_manager.py init --west-yml west.yml --clean-old
  3. 同步镜像（输出 DEBUG 日志）：
     python zephyr_mirror_manager.py sync --log-level DEBUG
  4. 同步镜像（跳过指定仓库）：
     python zephyr_mirror_manager.py sync --skip-repos old_repo.git test_repo.git
        """
    )

    # 全局参数
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"],
        help="日志级别（默认：INFO）"
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help="日志文件路径（默认：zephyr_mirror_manager.log）"
    )

    # 子命令解析器
    subparsers = parser.add_subparsers(dest="command", required=True, help="子命令")

    # init 子命令：生成镜像
    parser_init = subparsers.add_parser("init", help="初始化 Zephyr 本地镜像")
    parser_init.add_argument(
        "--west-yml",
        help="指定要解析的 west.yml 文件路径（如果不指定，则从当前目录递归搜索 .git 仓库）"
    )
    parser_init.add_argument(
        "--mirror-root",
        default=DEFAULT_MIRROR_ROOT,
        help=f"镜像根目录（默认：{DEFAULT_MIRROR_ROOT}）"
    )
    parser_init.add_argument(
        "--clean-old",
        action="store_true",
        help="是否清理旧镜像目录（默认：不清理）"
    )
    parser_init.add_argument(
        "--skip-dirs",
        nargs="+",
        default=list(DEFAULT_SKIP_DIRS),
        help=f"跳过的遍历目录（默认：{DEFAULT_SKIP_DIRS}）"
    )


    # sync 子命令：同步镜像
    parser_sync = subparsers.add_parser("sync", help="同步镜像到远程最新版本")
    parser_sync.add_argument(
        "--mirror-root",
        default=DEFAULT_MIRROR_ROOT,
        help=f"镜像根目录（默认：{DEFAULT_MIRROR_ROOT}）"
    )
    parser_sync.add_argument(
        "--skip-repos",
        nargs="+",
        default=list(DEFAULT_SKIP_REPOS),
        help=f"跳过的仓库名（默认：无）"
    )

    # 2. 解析参数
    args = parser.parse_args()

    # 3. 配置日志
    logger = setup_logger(args.log_level, args.log_file)

    # 4. 调度功能
    if args.command == "init":
        init_mirror(
            mirror_root=args.mirror_root,
            west_yml_path=args.west_yml,
            clean_old=args.clean_old,
            skip_dirs=set(args.skip_dirs),
        )
    elif args.command == "sync":
        sync_mirror(
            mirror_root=args.mirror_root,
            skip_repos=set(args.skip_repos),
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.getLogger("zephyr_mirror_manager").warning("用户手动中断操作")
        sys.exit(0)
    except Exception as e:
        logging.getLogger("zephyr_mirror_manager").critical(f"脚本执行出错：{str(e)}", exc_info=True)
        sys.exit(1)