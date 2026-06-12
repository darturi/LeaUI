export const projectTagColors = [
  'border-sky-200 bg-sky-100 text-sky-800',
  'border-emerald-200 bg-emerald-100 text-emerald-800',
  'border-amber-200 bg-amber-100 text-amber-800',
  'border-rose-200 bg-rose-100 text-rose-800',
  'border-violet-200 bg-violet-100 text-violet-800',
  'border-cyan-200 bg-cyan-100 text-cyan-800',
];

export function projectTagClass(projectTitle) {
  const colorIndex = Array.from(projectTitle).reduce(
    (sum, character) => sum + character.charCodeAt(0),
    0,
  ) % projectTagColors.length;
  return projectTagColors[colorIndex];
}
