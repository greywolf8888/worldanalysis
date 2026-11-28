"""Install the reviewed edition at the reader's existing Obsidian entry."""
from datetime import datetime, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote

from course_companions import ORIGINALS, course_file_references, image_outputs, verify_published_companions
from project import ENTRY, copy_file, file_lock, local_child, named_entry, put, read, sha
from publish import verify_links
from reader_review import chapters
from vault_catalog import refresh_catalog


DELIVERY = "obsidian-delivery.json"
ENTRY_PATH = ENTRY.as_posix()
RENDER_DIR = "日常库接口验收"
RENDER_METHOD = "日常入口同版快照的 Obsidian 接口重开"


def obsidian_uri(entry):
    return "obsidian://open?path=" + quote(entry.as_posix(), safe="")


def delivery(project):
    path = project.ledger / DELIVERY
    return read(path) if path.is_file() else None


def destination(vault, folder):
    vault = Path(vault).resolve(strict=True)
    if not local_child(vault, ".obsidian").is_dir():
        raise ValueError(f"请选择实际使用的 Obsidian 库 {vault}")
    if Path(folder).name != folder or folder.startswith("."):
        raise ValueError("阅读库课程目录须为一个普通的一级目录名")
    return vault, local_child(vault, folder)


def linked_originals(project, body):
    originals = {row["file"]: row["sha256"] for row in read(project.intake)["sources"]}
    references = {}
    for name in dict.fromkeys(course_file_references(body)):
        if not name.startswith(f"{ORIGINALS}/"):
            continue
        relative = name.removeprefix(f"{ORIGINALS}/")
        if relative not in originals:
            raise ValueError(f"阅读库资料入口未登记 {relative}")
        references[name] = originals[relative]
    return references


def verify_installed(project, publication, state):
    _, target = destination(state["vault"], state["folder"])
    entry = Path(state["entry"])
    if entry not in (named_entry(target), local_child(target, ENTRY)):
        raise ValueError("阅读库教材路径不在已绑定的课程目录内")
    expected = publication["outputs"][ENTRY_PATH]
    if state["sha256"] != expected:
        raise ValueError("阅读库还未交付课程包中的这一版")
    if {path for path in entry.parent.rglob("*.md")} != {entry}:
        raise ValueError("阅读库本课需要保留唯一教材入口")
    files = {**publication["outputs"], **linked_originals(project, publication["body"])}
    for name, digest in files.items():
        path = entry if name == ENTRY_PATH else local_child(target, name)
        if not path.is_file() or sha(path) != digest:
            raise ValueError(f"阅读库的文件与验收版本不一致 {path}")
    verify_published_companions(target, publication)
    verify_links(target, {entry.relative_to(target).as_posix(): publication["body"]}, art_root=target)
    return entry


def check_legacy_links(vault, entry):
    legacy = ENTRY.stem
    for note in vault.rglob("*.md"):
        if note == entry or note.is_symlink() or any(part.startswith(".") for part in note.relative_to(vault).parts):
            continue
        text = unquote(note.read_text(encoding="utf-8-sig", errors="replace"))
        links = re.findall(r"\[\[([^\]]+)\]\]|\]\(([^)]+)\)", text)
        if any(legacy in (wikilink or markdown) for wikilink, markdown in links):
            raise ValueError(f"旧笔记有入链，请先在 Obsidian 内重命名以更新引用 {note}")


def install_new_folder(project, vault, target, entry, body, assets, digest):
    # One folder arrival lets the open reader index a complete course rather than a burst of partial files.
    config = local_child(vault, ".obsidian")
    staging_parent = vault.parent if vault.parent != vault and vault.parent.stat().st_dev == vault.stat().st_dev else config
    try:
        staging = TemporaryDirectory(prefix=".worldanalysis-course-", dir=staging_parent)
    except PermissionError:
        if staging_parent == config:
            raise
        staging = TemporaryDirectory(prefix=".worldanalysis-course-", dir=config)
    with staging as scratch:
        staged = local_child(Path(scratch), target.name)
        relative_entry = entry.relative_to(target)
        for name, fingerprint in assets.items():
            copy_file(local_child(project.folder, name), local_child(staged, name), fingerprint)
        copy_file(project.entry, local_child(staged, relative_entry), digest)
        verify_links(staged, {relative_entry.as_posix(): body}, art_root=staged)
        with file_lock(local_child(vault, ".obsidian/worldanalysis-install.lock"), blocking=True):
            if target.exists():
                raise ValueError(f"交付期间出现同名项目目录，请重新核对后续作 {target}")
            staged.rename(target)


def update_existing_folder(project, target, entry, current, old_sha, body, assets, digest):
    missing = []
    for name, fingerprint in assets.items():
        asset = local_child(target, name)
        if not asset.exists():
            missing.append((name, fingerprint))
        elif not asset.is_file() or sha(asset) != fingerprint:
            raise ValueError(f"阅读库已有同名但不同的文件，请先核对 {asset}")

    if old_sha and (old_sha != digest or current != entry):
        archive = local_child(project.ledger, f"obsidian-previous/{old_sha}/{current.name}")
        copy_file(current, archive, old_sha)
    for name, fingerprint in missing:
        asset = local_child(target, name)
        if asset.exists() and (not asset.is_file() or sha(asset) != fingerprint):
            raise ValueError(f"交付期间出现不同的同名文件 {asset}")
        copy_file(local_child(project.folder, name), asset, fingerprint)
    current_sha = sha(current) if current.is_file() else None
    if current_sha != old_sha:
        raise ValueError("交付期间阅读库教材已变更，请保留并对照修改")
    verify_links(target, {entry.relative_to(target).as_posix(): body}, art_root=target)
    if current != entry:
        if entry.exists():
            raise ValueError(f"交付期间出现同名教材 {entry}")
        current.rename(entry)
    copy_file(project.entry, entry, digest)
    return not missing


def install(project, publication, vault=None, folder=None, reviewed_previous=None):
    previous = delivery(project)
    if vault is None and previous is None:
        raise ValueError("首次交付请指定日常阅读所用的 Obsidian 库")
    vault, target = destination(vault or previous["vault"],
                                folder or (previous["folder"] if previous else project.folder.name))
    binding = {"vault": str(vault), "folder": target.name}
    if previous and any(previous[key] != value for key, value in binding.items()):
        raise ValueError("课程已经绑定阅读库入口，请沿同一入口更新")
    if target != project.folder and (target.is_relative_to(project.folder)
                                     or project.folder.is_relative_to(target)
                                     or project.folder.is_relative_to(vault)):
        raise ValueError("请选择独立阅读库或沿课程包在该库中的既有入口交付")

    entry = named_entry(target)
    legacy = local_child(target, ENTRY)
    if legacy != entry and legacy.exists() and entry.exists():
        raise ValueError("阅读库同时存在旧入口和同名教材，请先核对两篇内容")
    current = legacy if legacy.is_file() else entry
    expected = publication["outputs"][ENTRY_PATH]
    old_sha = sha(current) if current.is_file() else None
    if old_sha and old_sha not in {expected, reviewed_previous, previous["sha256"] if previous else None}:
        raise ValueError(f"阅读库已有不同的教材，请核对并保留读者修改后再接管 {current} ({old_sha})")
    if set(entry.parent.rglob("*.md")) - {current}:
        raise ValueError("阅读库本课有其他 Markdown，请先核对唯一教材入口")
    if current != entry:
        check_legacy_links(vault, current)

    verify_published_companions(project.folder, publication)
    assets = {**publication["outputs"], **linked_originals(project, publication["body"])}
    assets.pop(ENTRY_PATH)
    if target.exists():
        assets_unchanged = update_existing_folder(
            project, target, entry, current, old_sha, publication["body"], assets, expected)
    else:
        install_new_folder(project, vault, target, entry, publication["body"], assets, expected)
        assets_unchanged = False

    verify_published_companions(target, publication)

    already_installed = (previous and previous["entry"] == str(entry) and previous["sha256"] == expected
                         and old_sha == expected and assets_unchanged)
    if already_installed:
        state = previous
    else:
        state = {**binding, "entry": str(entry), "uri": obsidian_uri(entry), "sha256": expected,
                 "installed_at": datetime.now(timezone.utc).isoformat(), "observation": None}
        put(project.ledger / DELIVERY, state)
    # Register this delivery even before reader confirmation, so an intervening
    # empty-context refresh cannot mistake it for a scope-accepted edition.
    refresh_catalog(vault, packages=(project.folder,))
    return state


def confirm(project, publication, observed):
    state = delivery(project)
    if not state:
        raise ValueError("请先交付到日常 Obsidian 阅读库")
    entry = verify_installed(project, publication, state)
    images = image_outputs(publication)
    if (observed.get("entry") != str(entry) or observed.get("sha256") != state["sha256"]
            or observed.get("installed_at") != state["installed_at"]
            or observed.get("mode") != "preview" or not all(
                observed.get(key) is True for key in ("reopened", "reached_bottom", "references_collapsed")
            ) or observed.get("visible_citations") is not False
            or observed.get("section_seen") not in chapters(publication["body"])
            or images and observed.get("image_seen") not in images
            or not observed.get("note", "").strip()):
        raise ValueError("需记录同一版教材在日常 Obsidian 库重开后的章节、配图和阅读结果")
    state["observation"] = observed
    put(project.ledger / DELIVERY, state)
    refresh_catalog(Path(state["vault"]), packages=(project.folder,))
    return state


def automated_observation(project, publication, state, render_file):
    from acceptance import check_render

    entry = verify_installed(project, publication, state)
    render = read(render_file)
    check_render(render, state["sha256"],
                 companions=publication.get("review", {}).get("snapshot", {}).get("companions"),
                 require_companion_reading="companion_reading" in publication.get("obsidian", {}))
    view = render["checks"][0]
    relative = f"{state['folder']}/{entry.relative_to(entry.parent.parent).as_posix()}"
    sections = tuple(chapters(publication["body"]))
    files = image_outputs(publication)
    rendered_images = {f"{ENTRY.parent.as_posix()}/原图/{item['name']}" for item in view["images"]}
    if (view["file"] != relative or not view.get("reopened")
            or not sections or any(section not in view["sections"] for section in sections)
            or not files.issubset(rendered_images)):
        raise ValueError("Obsidian 应重开日常交付同路径快照并读取所有章节与图片")
    return {
        **{key: state[key] for key in ("entry", "sha256", "installed_at")},
        "method": RENDER_METHOD,
        "render_sha256": sha(render_file),
        "mode": "preview", "reopened": True, "reached_bottom": True, "references_collapsed": True,
        "visible_citations": False, "section_seen": sections[-1], "image_seen": next(iter(sorted(files)), None),
        "note": "直接读取日常库中的交付文件，在独立 Obsidian 环境用接口重开同路径副本，由章首核阅至折叠出处和页尾。",
    }


def delivery_status(project, publication, required=False):
    state = delivery(project)
    if state and state["sha256"] == publication["outputs"][ENTRY_PATH]:
        try:
            entry = verify_installed(project, publication, state)
        except (OSError, ValueError) as error:
            if required:
                raise
            return {"status": "阅读库文件待核对", "entry": state["entry"], "detail": str(error)}
        observation = state["observation"]
        if observation and observation.get("method") == RENDER_METHOD:
            render = local_child(project.work, f"{RENDER_DIR}/{state['sha256']}.json")
            if not render.is_file() or sha(render) != observation.get("render_sha256"):
                observation = None
        result = {"status": "已记录日常库重开" if observation else "待在日常库重开",
                  "entry": state["entry"], "uri": obsidian_uri(entry), "sha256": state["sha256"],
                  "method": observation.get("method", "日常窗口观察") if observation else None}
        if required and not observation:
            raise ValueError(f"已更新 {entry}，尚待 Obsidian 重开核验")
        return result
    if required:
        raise ValueError("当前课程包版本尚未交付到日常 Obsidian 库")
    return {"status": "阅读库待更新" if state else "尚未交付日常阅读库"}
