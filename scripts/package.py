"""Build a source/example archive without runtime state, private data or junctions."""
import argparse
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = {'zhishi','web','tests','scripts','docs','integrations','workflows','projects'}
EXCLUDE_DIRS = {'node_modules','__pycache__','模拟收件箱','样例预览','.git'}
SOURCE_SUFFIXES = {'.py','.mjs','.js','.html','.css','.md','.ps1','.yaml','.json','.xlsx','.csv','.png'}
ROOT_FILES = {
    'app.py','workflow.py','wechat_bridge.py','wechat_connect.py','wechat.ps1','start.ps1',
    '.gitignore','.env.example','litellm.config.example.yaml',
}


def source_files():
    for file in ROOT.iterdir():
        if file.is_file() and (file.name in ROOT_FILES or file.suffix == '.md'):
            yield file
    for name in sorted(SOURCE_DIRS):
        for directory, dirs, files in os.walk(ROOT/name, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not (Path(directory)/d).is_symlink())
            for filename in sorted(files):
                file = Path(directory)/filename
                if file.is_symlink() or file.suffix not in SOURCE_SUFFIXES:
                    continue
                if filename.endswith(('.inspection.json','.inspect.ndjson')):
                    continue
                yield file


def main():
    parser = argparse.ArgumentParser(description='打包源码与示例，排除私有运行数据')
    parser.add_argument('--output', type=Path, default=ROOT.parent/'wechat-assistant-v01.zip')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(source_files())
    with zipfile.ZipFile(args.output,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, Path(ROOT.name)/file.relative_to(ROOT))
    print(f'已打包 {len(files)} 个源码／示例文件：{args.output}')


if __name__ == '__main__':
    main()
