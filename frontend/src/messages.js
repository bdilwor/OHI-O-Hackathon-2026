// Chat message factory shared by App (welcome messages) and Workspace.
let nextId = 1;
export const msg = (m) => ({ id: `m${nextId++}`, ...m });
