// The Images pane: the chosen folders and the images in them, with a tag
// (tampered or untampered) and a remove button on every folder and image.
// `tree` comes from buildTree; the parent owns the files and tags.
import { useState } from "react";
import { TAGS, describeCounts, shownLabel } from "./fileTree.js";

export default function FileTree({ tree, onTag, onRemove }) {
  const [collapsed, setCollapsed] = useState(() => new Set());
  const toggle = (key) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  return (
    <div className="file-tree">
      <ul>
        {tree.children.map((node) => (
          <Node key={node.type === "folder" ? node.key : node.path} node={node} collapsed={collapsed} toggle={toggle} onTag={onTag} onRemove={onRemove} />
        ))}
      </ul>
    </div>
  );
}

function Node({ node, collapsed, toggle, onTag, onRemove }) {
  if (node.type === "file") {
    return (
      <li className="tree-row">
        <span className="tree-name" title={node.path}>
          {node.name}
        </span>
        <LabelBadge label={node.label} source={node.source} />
        <TagButtons subject={node.path} target={node.path} active={node.tag} onTag={onTag} />
        <button type="button" className="link danger" aria-label={`Remove ${node.path}`} onClick={() => onRemove(node.path)}>
          Remove
        </button>
      </li>
    );
  }

  const open = !collapsed.has(node.key);
  const path = node.key.slice(0, -1);
  return (
    <li>
      <div className="tree-row">
        <button type="button" className="link tree-name" aria-expanded={open} title={path} onClick={() => toggle(node.key)}>
          <span aria-hidden="true">{open ? "▾" : "▸"}</span> {node.name}
        </button>
        <span className="muted small">
          {node.count} image{node.count === 1 ? "" : "s"}: {describeCounts(node.labels)}
        </span>
        <TagButtons subject={`folder ${path}`} target={node.key} active={node.tag} onTag={onTag} />
        <button type="button" className="link danger" aria-label={`Remove folder ${path} and its ${node.count} image(s)`} onClick={() => onRemove(node.key)}>
          Remove
        </button>
      </div>
      {open && (
        <ul>
          {node.children.map((child) => (
            <Node key={child.type === "folder" ? child.key : child.path} node={child} collapsed={collapsed} toggle={toggle} onTag={onTag} onRemove={onRemove} />
          ))}
        </ul>
      )}
    </li>
  );
}

// Pressing the tag that is already set clears it.
function TagButtons({ subject, target, active, onTag }) {
  return (
    <span className="tag-buttons" role="group" aria-label={`Tag ${subject}`}>
      {TAGS.map((tag) => (
        <button key={tag} type="button" className={`tag ${tag}${active === tag ? " on" : ""}`} aria-pressed={active === tag} onClick={() => onTag(target, active === tag ? null : tag)}>
          {tag}
        </button>
      ))}
    </span>
  );
}

const SOURCE_NOTE = { folder: "from its folder's tag", name: "from its folder's name" };

// What the image will be scored as; dashed when it is not tagged on the image itself.
function LabelBadge({ label, source }) {
  const shown = shownLabel(label);
  const inherited = source === "folder" || source === "name";
  return (
    <span
      className={`badge ${label ? shown : ""}${inherited ? " inherited" : ""}`}
      title={SOURCE_NOTE[source] ?? (label ? "tagged on this image" : "not scored for accuracy")}
    >
      {shown}
    </span>
  );
}
