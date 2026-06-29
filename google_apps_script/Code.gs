function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Encuentro Noticias')
    .addItem('Lanzar búsqueda', 'launchSearch')
    .addItem('Consultar estado', 'checkRunStatus')
    .addSeparator()
    .addItem('Publicar reseñas', 'publishReviews')
    .addItem('Consultar publicación', 'checkPublishStatus')
    .addSeparator()
    .addItem('Indexar fuentes', 'indexSources')
    .addItem('Consultar indexación', 'checkIndexStatus')
    .addSeparator()
    .addItem('Limpiar logs antiguos', 'cleanupLogs')
    .addItem('Limpiar descartes antiguos', 'cleanupDescartes')
    .addItem('Limpiar filas vacías de publicación', 'cleanupEmptyPublicationRows')
    .addToUi();
}

/**
 * Obtiene el valor de una clave de configuración desde la pestaña 'Config'.
 */
function getConfigValue(key) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var configSheet = ss.getSheetByName("Config");
  if (!configSheet) return null;
  var data = configSheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (data[i][0] === key) {
      return data[i][1];
    }
  }
  return null;
}

/**
 * Formatea un valor que puede ser Date o texto a formato YYYY-MM-DD.
 */
function formatDateValue_(value) {
  if (!value) return '';
  if (Object.prototype.toString.call(value) === '[object Date]' && !isNaN(value)) {
    return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  return String(value).trim();
}

/**
 * Lanza una búsqueda de noticias/reseñas desde el panel leyendo la configuración.
 */
function launchSearch() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'. Ejecute primero el endpoint /setup/ensure-sheet.");
    return;
  }
  
  // Leer inputs del Panel
  var dateMinRaw = panelSheet.getRange("B3").getValue();
  var dateMaxRaw = panelSheet.getRange("B4").getValue();
  var dateMin = formatDateValue_(dateMinRaw);
  var dateMax = formatDateValue_(dateMaxRaw);
  
  var maxBooksRaw = panelSheet.getRange("B5").getValue();
  var dryRun = panelSheet.getRange("B6").getValue();
  var includeUnknown = panelSheet.getRange("B7").getValue();
  
  // Normalizar booleanos en caso de que sean strings
  if (typeof dryRun === "string") {
    dryRun = dryRun.toUpperCase() === "TRUE";
  }
  if (typeof includeUnknown === "string") {
    includeUnknown = includeUnknown.toUpperCase() === "TRUE";
  }
  
  // Validar formato de fechas
  var dateReg = /^\d{4}-\d{2}-\d{2}$/;
  if (dateMin && !dateReg.test(dateMin)) {
    SpreadsheetApp.getUi().alert("Error: La 'Fecha mínima' debe tener formato YYYY-MM-DD.");
    return;
  }
  if (dateMax && !dateReg.test(dateMax)) {
    SpreadsheetApp.getUi().alert("Error: La 'Fecha máxima' debe tener formato YYYY-MM-DD.");
    return;
  }
  
  // Obtener URL del backend y token desde la pestaña Config
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  // Quitar barra diagonal final si existe
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  // Construir payload esperado por el backend
  var payload = {
    "dry_run": dryRun !== false,
    "date_min": dateMin || null,
    "date_max": dateMax || null,
    "include_unknown_dates": includeUnknown !== false
  };
  
  // Solo enviar limit_books si B5 no está vacío o nulo
  if (maxBooksRaw !== "" && maxBooksRaw !== null && maxBooksRaw !== undefined) {
    var num = Number(maxBooksRaw);
    if (!isNaN(num) && num > 0) {
      payload.limit_books = num;
    }
  }
  
  var headers = {
    "Content-Type": "application/json"
  };
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };
  
  try {
    panelSheet.getRange("B8").setValue("Iniciando...");
    var response = UrlFetchApp.fetch(backendUrl + "/runs", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var runId = data.run_id || data.id || "";
      
      panelSheet.getRange("B8").setValue("running");
      panelSheet.getRange("B9").setValue(runId);
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Búsqueda iniciada correctamente.");
      
      SpreadsheetApp.getUi().alert("Búsqueda iniciada correctamente.\nBúsqueda ID: " + runId + "\nPuede refrescar el estado usando el botón 'Consultar estado'.");
    } else {
      panelSheet.getRange("B8").setValue("error");
      panelSheet.getRange("B11").setValue("HTTP " + resCode + ": " + resText);
      SpreadsheetApp.getUi().alert("Error en el backend (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    panelSheet.getRange("B8").setValue("error");
    panelSheet.getRange("B11").setValue(e.toString());
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado de la última búsqueda indicada en B9 y actualiza el panel.
 */
function checkRunStatus() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var runId = panelSheet.getRange("B9").getValue().toString().trim();
  if (!runId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún 'Última búsqueda_id' registrado en la celda B9.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var headers = {};
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "get",
    "headers": headers,
    "muteHttpExceptions": true
  };
  
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/runs/" + runId, options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode === 200) {
      var data = JSON.parse(resText);
      var status = data.status || "";
      var processed = data.books_processed || 0;
      var total = data.books_total || 0;
      var msg = data.message || "";
      
      panelSheet.getRange("B8").setValue(status);
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Procesados: " + processed + "/" + total + " — " + msg);
      
      SpreadsheetApp.getActiveSpreadsheet().toast("Estado de la búsqueda " + runId + " actualizado: " + status, "Encuentro Noticias", 5);
    } else {
      SpreadsheetApp.getUi().alert("Error al consultar estado (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error al conectar:\n" + e.toString());
  }
}

/**
 * Publica las reseñas marcadas en la pestaña 'Reseñas por publicar' hacia WordPress.
 */
function publishReviews() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var dryRun = panelSheet.getRange("B6").getValue();
  if (typeof dryRun === "string") {
    dryRun = dryRun.toUpperCase() === "TRUE";
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var payload = {
    "dry_run": dryRun !== false
  };
  
  var headers = {
    "Content-Type": "application/json"
  };
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };
  
  try {
    panelSheet.getRange("B8").setValue("Publicando...");
    var response = UrlFetchApp.fetch(backendUrl + "/publish/reviews", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Publicación completada.";
      var pubCount = data.published_count || 0;
      var errCount = data.errors_count || 0;
      var skipCount = data.unselected_count || 0;
      
      panelSheet.getRange("B8").setValue("completed");
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Publicadas: " + pubCount + ", Errores: " + errCount + ", No marcadas: " + skipCount);
      
      SpreadsheetApp.getUi().alert("Publicación completada.\n" + msg + "\n\nResumen:\n- Publicadas: " + pubCount + "\n- Errores: " + errCount + "\n- No marcadas: " + skipCount);
    } else {
      panelSheet.getRange("B8").setValue("error");
      panelSheet.getRange("B11").setValue("HTTP " + resCode + ": " + resText);
      SpreadsheetApp.getUi().alert("Error al publicar (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    panelSheet.getRange("B8").setValue("error");
    panelSheet.getRange("B11").setValue(e.toString());
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado de publicación (para complementar la interfaz).
 */
function checkPublishStatus() {
  SpreadsheetApp.getUi().alert("El proceso de publicación se ejecuta síncronamente y actualiza el panel al instante al terminar.");
}

/**
 * Inicia la indexación manual de fuentes en segundo plano.
 */
function indexSources() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var payload = {
    "limit_domains": 10,
    "force_refresh": true
  };
  
  var headers = {
    "Content-Type": "application/json"
  };
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };
  
  try {
    panelSheet.getRange("B8").setValue("Indexando...");
    var response = UrlFetchApp.fetch(backendUrl + "/sources/index", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var jobId = data.job_id || "";
      
      panelSheet.getRange("B8").setValue("running");
      panelSheet.getRange("B9").setValue(jobId);
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Indexación de fuentes iniciada en segundo plano.");
      
      SpreadsheetApp.getUi().alert("Indexación iniciada correctamente.\nJob ID: " + jobId + "\nPuede consultar el progreso con 'Consultar indexación'.");
    } else {
      panelSheet.getRange("B8").setValue("error");
      panelSheet.getRange("B11").setValue("HTTP " + resCode + ": " + resText);
      SpreadsheetApp.getUi().alert("Error al indexar (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    panelSheet.getRange("B8").setValue("error");
    panelSheet.getRange("B11").setValue(e.toString());
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado del job de indexación de fuentes indicado en B9.
 */
function checkIndexStatus() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var jobId = panelSheet.getRange("B9").getValue().toString().trim();
  if (!jobId || jobId.indexOf("idx_") !== 0) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún Job ID de indexación (empieza por idx_) en la celda B9.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var headers = {};
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "get",
    "headers": headers,
    "muteHttpExceptions": true
  };
  
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/sources/index/status?job_id=" + jobId, options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode === 200) {
      var data = JSON.parse(resText);
      var status = data.status || "";
      var comp = data.domains_completed || 0;
      var total = data.domains_total || 0;
      var urls = data.urls_found || 0;
      var errs = (data.errors || []).length;
      
      panelSheet.getRange("B8").setValue(status);
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Dominios: " + comp + "/" + total + " — URLs encontradas: " + urls + " — Errores: " + errs);
      
      SpreadsheetApp.getActiveSpreadsheet().toast("Estado de indexación actualizado: " + status, "Encuentro Noticias", 5);
    } else {
      SpreadsheetApp.getUi().alert("Error al consultar indexación (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error al conectar:\n" + e.toString());
  }
}

/**
 * Limpia los logs antiguos del Sheet manteniendo el límite seguro de filas / retención.
 */
function cleanupLogs() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var headers = {};
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "muteHttpExceptions": true
  };
  
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/logs/cleanup", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Limpieza completada.";
      var deleted = data.deleted_count || 0;
      var remaining = data.remaining_count || 0;
      
      panelSheet.getRange("B10").setValue(new Date().toISOString().replace("T", " ").slice(0, 19));
      panelSheet.getRange("B11").setValue("Logs limpiados: " + deleted + " eliminados, quedan: " + remaining);
      
      SpreadsheetApp.getUi().alert("Limpieza de logs completada.\n" + msg + "\n\nDetalle:\n- Logs eliminados: " + deleted + "\n- Logs restantes: " + remaining);
    } else {
      SpreadsheetApp.getUi().alert("Error al limpiar logs (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Limpia los descartes antiguos del Sheet manteniendo el límite seguro de filas / retención.
 */
function cleanupDescartes() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var headers = {};
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "muteHttpExceptions": true
  };
  
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/descartes/cleanup", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Limpieza completada.";
      var deleted = data.deleted_rows || 0;
      var remaining = data.remaining_rows || 0;
      
      SpreadsheetApp.getUi().alert("Limpieza de descartes completada.\n" + msg + "\n\nDetalle:\n- Descartes eliminados: " + deleted + "\n- Descartes restantes: " + remaining);
    } else {
      SpreadsheetApp.getUi().alert("Error al limpiar descartes (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Limpia las filas vacías de publicación (filas falsas) en Reseñas por publicar.
 */
function cleanupEmptyPublicationRows() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = ss.getSheetByName("Panel");
  if (!panelSheet) {
    SpreadsheetApp.getUi().alert("Error: No se encontró la pestaña 'Panel'.");
    return;
  }
  
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  
  if (!backendUrl) {
    SpreadsheetApp.getUi().alert("Error: Configure 'BACKEND_BASE_URL' en la pestaña 'Config'.");
    return;
  }
  
  if (backendUrl.slice(-1) === "/") {
    backendUrl = backendUrl.slice(0, -1);
  }
  
  var headers = {};
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  
  var options = {
    "method": "post",
    "headers": headers,
    "muteHttpExceptions": true
  };
  
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/reviews/cleanup-empty-publication-rows", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Limpieza completada.";
      
      var detailStr = "";
      if (data.cleaned_details) {
        detailStr = "\n\nDetalle por pestaña:\n";
        for (var key in data.cleaned_details) {
          detailStr += "- " + key + ": " + data.cleaned_details[key] + " filas limpiadas\n";
        }
      } else {
        var cleaned = data.cleaned_rows || 0;
        detailStr = "\n\nDetalle:\n- Filas limpiadas: " + cleaned;
      }
      
      SpreadsheetApp.getUi().alert("Limpieza de filas vacías completada.\n" + msg + detailStr);
    } else {
      SpreadsheetApp.getUi().alert("Error al limpiar filas vacías (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}
