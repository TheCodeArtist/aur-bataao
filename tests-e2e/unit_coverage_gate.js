export function coverageGateError(output) {
  const text = String(output);
  if (/code coverage could not be enabled/i.test(text)) {
    return "JavaScript unit coverage could not be enabled; the coverage gate is invalid.";
  }
  if (
    !text.includes("start of coverage report")
    || !text.includes("end of coverage report")
  ) {
    return "JavaScript unit tests finished without a complete coverage report.";
  }
  return null;
}
