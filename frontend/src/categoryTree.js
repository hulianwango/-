export function splitCategoryPath(categoryPath) {
  return String(categoryPath || "")
    .split("/")
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function createCategoryNode(path, label, order) {
  return {
    path,
    label,
    order,
    paper_count: 0,
    subtree_count: 0,
    children: [],
  };
}

export function buildCategoryTree(categories) {
  const root = createCategoryNode("", "全部分类", -1);
  const nodesByPath = new Map([["", root]]);
  let nextOrder = 0;

  for (const category of categories || []) {
    const categoryPath = String(category.category_path || "");
    const segments = splitCategoryPath(categoryPath);
    if (!segments.length) {
      root.label = category.category_label || "全部分类";
      root.paper_count = Number(category.paper_count) || 0;
      continue;
    }

    let parent = root;
    let currentPath = "";
    for (const segment of segments) {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      let node = nodesByPath.get(currentPath);
      if (!node) {
        node = createCategoryNode(currentPath, segment, nextOrder);
        nextOrder += 1;
        nodesByPath.set(currentPath, node);
        parent.children.push(node);
      }
      parent = node;
    }

    parent.label = category.category_label || segments[segments.length - 1] || categoryPath;
    parent.paper_count = Number(category.paper_count) || 0;
  }

  const finalizeNode = (node) => {
    node.children.sort((left, right) => left.order - right.order || left.path.localeCompare(right.path));
    node.subtree_count =
      node.paper_count + node.children.reduce((total, child) => total + finalizeNode(child), 0);
    return node.subtree_count;
  };
  finalizeNode(root);

  return { root, nodesByPath };
}
