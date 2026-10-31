// 计算控制面板的基径（以 "/" 开头并以 "/" 结尾）
// 面板是单入口应用，文档地址即挂载点：根路径部署为 "/"，反代子路径部署为
// "/AGY2API/" 等形式。部分反代不补齐末尾斜杠，访问地址可能为 "/AGY2API"，
// 此处统一补齐，保证 API 请求与页面跳转落在同一子路径下
export function getBasePath() {
  const { pathname } = window.location;
  return pathname.endsWith("/") ? pathname : pathname + "/";
}
