function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Encuentro Noticias')
    .addItem('Lanzar búsqueda', 'launchSearch')
    .addItem('Consultar estado búsqueda', 'checkRunStatus')
    .addItem('Cancelar búsqueda', 'cancelSearch')
    .addSeparator()
    .addItem('Publicar reseñas', 'publishReviews')
    .addItem('Consultar estado publicación', 'checkPublishStatus')
    .addItem('Cancelar publicación', 'cancelPublish')
    .addSeparator()
    .addItem('Indexar fuentes', 'indexSources')
    .addItem('Consultar estado indexación', 'checkIndexStatus')
    .addSeparator()
    .addItem('Borrar logs', 'cleanupLogs')
    .addItem('Borrar descartes', 'cleanupDescartes')
    .addItem('Limpiar filas vacías de publicación', 'cleanupEmptyPublicationRows')
    .addSeparator()
    .addItem('Autorizar seguimiento automático', 'autorizarSeguimientoAutomatico')
    .addToUi();
}

function getConfigValue(key) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  
  // Try Config first
  var configSheet = ss.getSheetByName("Config");
  if (configSheet) {
    var data = configSheet.getDataRange().getValues();
    for (var i = 1; i < data.length; i++) {
      if (data[i][0] === key) {
        return data[i][1];
      }
    }
  }
  
  // Try Config técnica fallback
  var techSheet = ss.getSheetByName("Config técnica");
  if (techSheet) {
    var dataTech = techSheet.getDataRange().getValues();
    for (var j = 1; j < dataTech.length; j++) {
      if (dataTech[j][0] === key) {
        return dataTech[j][1];
      }
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
 * Traduce el estado técnico a texto amigable para el usuario.
 */
function getFriendlyStatus_(status, defaultProgressText) {
  if (!status) return "";
  var s = status.toLowerCase();
  if (s === "pending") return "Pendiente";
  if (s === "running") return defaultProgressText || "Procesando...";
  if (s === "completed") return "Completado";
  if (s === "failed") return "Error";
  if (s === "cancelled" || s === "canceled") return "Cancelado";
  return status;
}

/**
 * Obtiene de forma segura la hoja 'Panel'.
 */
function getPanelSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Panel");
  if (!sheet) {
    throw new Error("No existe la pestaña Panel.");
  }
  return sheet;
}

/**
 * Helper centralizado para escribir estado y progreso en la pestaña Panel.
 */
function setPanelStatus_(estado, id, mensaje, resumen) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var panelSheet = getPanelSheet_();
  var now = Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), "yyyy-MM-dd HH:mm:ss");

  if (estado !== null && estado !== undefined) {
    panelSheet.getRange("F4").setValue(estado);
  }
  if (id !== null && id !== undefined) {
    panelSheet.getRange("F5").setValue(id);
  }
  panelSheet.getRange("F6").setValue(now);
  if (mensaje !== null && mensaje !== undefined) {
    panelSheet.getRange("F7").setValue(mensaje);
  }
  if (resumen !== null && resumen !== undefined) {
    panelSheet.getRange("B11").setValue(resumen);
  }
}

/**
 * Lanza una búsqueda de noticias/reseñas desde el panel leyendo la configuración.
 */
function launchSearch() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  // Leer inputs del Panel
  var dateMinRaw = panelSheet.getRange("C4").getValue();
  var dateMaxRaw = panelSheet.getRange("C5").getValue();
  var dateMin = formatDateValue_(dateMinRaw);
  var dateMax = formatDateValue_(dateMaxRaw);
  
  var maxBooksRaw = panelSheet.getRange("C6").getValue();
  var includeUnknown = panelSheet.getRange("C7").getValue();
  
  // MODO_PRUEBA se lee desde la pestaña Config
  var dryRun = getConfigValue("MODO_PRUEBA");
  
  // Normalizar booleanos en caso de que sean strings
  if (typeof dryRun === "string") {
    dryRun = dryRun.toUpperCase() === "TRUE" || dryRun.toUpperCase() === "VERDADERO" || dryRun === "1";
  } else {
    dryRun = dryRun === true;
  }
  if (typeof includeUnknown === "string") {
    includeUnknown = includeUnknown.toUpperCase() === "TRUE" || includeUnknown.toUpperCase() === "VERDADERO" || includeUnknown === "1";
  } else {
    includeUnknown = includeUnknown !== false;
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
    "dry_run": dryRun,
    "date_min": dateMin || null,
    "date_max": dateMax || null,
    "include_unknown_dates": includeUnknown
  };
  
  // Solo enviar limit_books si C6 no está vacío o nulo
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
    setPanelStatus_("Procesando...", null, "Iniciando búsqueda...", "Iniciando búsqueda...");
    
    var response = UrlFetchApp.fetch(backendUrl + "/runs", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var runId = data.run_id || data.id || "";
      
      PropertiesService.getScriptProperties().setProperty("LAST_SEARCH_RUN_ID", runId);
      var polling = tryScheduleAutoStatusCheck_("search");
      
      var msg = "Búsqueda iniciada correctamente.";
      var resumen = "Ejecución activa: búsqueda en segundo plano";
      if (!polling.ok) {
        msg = "Búsqueda iniciada correctamente. No se pudo activar el seguimiento automático. Usa 'Consultar estado' para actualizar manualmente.";
        resumen = "Seguimiento automático no disponible";
      }
      
      setPanelStatus_("Procesando...", runId, msg, resumen);
      
      var alertMsg = "Búsqueda iniciada correctamente.\nBúsqueda ID: " + runId;
      if (!polling.ok) {
        alertMsg += "\n\nAVISO: No se pudo programar el seguimiento automático (" + polling.error + ").\nPuede refrescar el estado usando el botón 'Consultar estado'.";
      } else {
        alertMsg += "\n\nPuede refrescar el estado usando el botón 'Consultar estado'.";
      }
      SpreadsheetApp.getUi().alert(alertMsg);
    } else {
      setPanelStatus_("Error", "", "HTTP " + resCode + ": " + resText, "Error al iniciar búsqueda.");
      SpreadsheetApp.getUi().alert("Error en el backend (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    setPanelStatus_("Error", "", e.toString(), "Error de conexión.");
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado de la última búsqueda indicada en PropertiesService o F5.
 */
function checkRunStatus() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var runId = PropertiesService.getScriptProperties().getProperty("LAST_SEARCH_RUN_ID");
  var cellVal = panelSheet.getRange("F5").getValue().toString().trim();
  if (cellVal.indexOf("run_") === 0) {
    runId = cellVal;
  }
  
  if (!runId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún 'Última búsqueda_id' registrado.");
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
      
      var friendlyStatus = getFriendlyStatus_(status, "Procesando...");
      var progressMsg = "Procesados: " + processed + "/" + total + " — " + msg;
      
      if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Ejecución cancelada");
      } else {
        var resumenTxt = "Ejecución activa: búsqueda en segundo plano";
        if (friendlyStatus === "Completado") {
          resumenTxt = "Ejecución completada con éxito";
        } else if (friendlyStatus === "Error") {
          resumenTxt = "Ejecución fallida con error";
        }
        setPanelStatus_(friendlyStatus, null, progressMsg, resumenTxt);
      }
      
      SpreadsheetApp.getActiveSpreadsheet().toast("Estado de la búsqueda " + runId + " actualizado: " + friendlyStatus, "Encuentro Noticias", 5);
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
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var dryRun = getConfigValue("MODO_PRUEBA");
  if (typeof dryRun === "string") {
    dryRun = dryRun.toUpperCase() === "TRUE" || dryRun.toUpperCase() === "VERDADERO" || dryRun === "1";
  } else {
    dryRun = dryRun === true;
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
  
  // Usar background = true para ejecución asíncrona
  var payload = {
    "dry_run": dryRun,
    "background": true
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
    setPanelStatus_("Procesando...", null, "Iniciando publicación...", "Iniciando publicación...");
    
    var response = UrlFetchApp.fetch(backendUrl + "/publish/reviews", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var publishId = data.publish_id || "";
      
      if (publishId) {
        PropertiesService.getScriptProperties().setProperty("LAST_PUBLISH_ID", publishId);
        var polling = tryScheduleAutoStatusCheck_("publish");
        
        var msg = "Proceso de publicación iniciado en segundo plano.";
        var resumen = "Ejecución activa: publicación en segundo plano";
        if (!polling.ok) {
          msg = "Publicación iniciada correctamente. No se pudo activar el seguimiento automático. Usa 'Consultar estado' para actualizar manualmente.";
          resumen = "Seguimiento automático no disponible";
        }
        
        setPanelStatus_("Procesando...", publishId, msg, resumen);
        
        var alertMsg = "Publicación iniciada correctamente.\nPublicación ID: " + publishId;
        if (!polling.ok) {
          alertMsg += "\n\nAVISO: No se pudo programar el seguimiento automático (" + polling.error + ").\nPuede refrescar el estado usando 'Consultar publicación'.";
        } else {
          alertMsg += "\n\nPuede refrescar el estado usando 'Consultar publicación'.";
        }
        SpreadsheetApp.getUi().alert(alertMsg);
      } else {
        var pubCount = data.published_count || 0;
        var errCount = data.errors_count || 0;
        
        setPanelStatus_("Completado", null, "Publicadas: " + pubCount + ", Errores: " + errCount, "Ejecución completada con éxito");
        SpreadsheetApp.getUi().alert("Publicación completada (síncrona).\n- Publicadas: " + pubCount + "\n- Errores: " + errCount);
      }
    } else {
      setPanelStatus_("Error", "", "HTTP " + resCode + ": " + resText, "Error al publicar.");
      SpreadsheetApp.getUi().alert("Error al publicar (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    setPanelStatus_("Error", "", e.toString(), "Error de conexión.");
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado de publicación en segundo plano usando LAST_PUBLISH_ID o el ID de F5.
 */
function checkPublishStatus() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var publishId = PropertiesService.getScriptProperties().getProperty("LAST_PUBLISH_ID");
  var cellVal = panelSheet.getRange("F5").getValue().toString().trim();
  if (cellVal.indexOf("pub_") === 0) {
    publishId = cellVal;
  }
  
  if (!publishId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún ID de publicación registrado.");
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
    var response = UrlFetchApp.fetch(backendUrl + "/publish/" + publishId + "/status", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode === 200) {
      var data = JSON.parse(resText);
      var status = data.status || "unknown";
      var pubCount = data.published_count || 0;
      var errCount = data.errors_count || 0;
      var msg = data.message || "";
      
      var friendlyStatus = getFriendlyStatus_(status, "Procesando...");
      var progressMsg = "Publicadas: " + pubCount + ", Errores: " + errCount + " — " + msg;
      
      if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Ejecución cancelada");
      } else {
        var resumenTxt = "Ejecución activa: publicación en segundo plano";
        if (friendlyStatus === "Completado") {
          resumenTxt = "Ejecución completada con éxito";
        } else if (friendlyStatus === "Error") {
          resumenTxt = "Ejecución fallida con error";
        }
        setPanelStatus_(friendlyStatus, null, progressMsg, resumenTxt);
      }
      
      if (friendlyStatus === "Completado" || friendlyStatus === "Cancelado" || friendlyStatus === "Error") {
        SpreadsheetApp.getUi().alert("Proceso de publicación finalizado.\nEstado: " + friendlyStatus + "\n" + progressMsg);
      } else {
        SpreadsheetApp.getActiveSpreadsheet().toast("Estado de publicación: " + friendlyStatus + " (" + pubCount + " publicadas)", "Info", 4);
      }
    } else {
      SpreadsheetApp.getUi().alert("Error al consultar publicación (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Envía una solicitud al backend para cancelar la búsqueda activa en PropertiesService o F5.
 */
function cancelSearch() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var runId = PropertiesService.getScriptProperties().getProperty("LAST_SEARCH_RUN_ID");
  var cellVal = panelSheet.getRange("F5").getValue().toString().trim();
  if (cellVal.indexOf("run_") === 0) {
    runId = cellVal;
  }
  
  if (!runId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún ID de búsqueda activo para cancelar.");
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
    var response = UrlFetchApp.fetch(backendUrl + "/runs/" + runId + "/cancel", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      deleteTriggersForType_("search");
      setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Ejecución cancelada por el usuario.");
      SpreadsheetApp.getUi().alert("La solicitud de cancelación de la búsqueda fue enviada al backend correctamente.");
    } else {
      SpreadsheetApp.getUi().alert("Error al cancelar la búsqueda (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Envía una solicitud al backend para cancelar la publicación activa.
 */
function cancelPublish() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var publishId = PropertiesService.getScriptProperties().getProperty("LAST_PUBLISH_ID");
  var cellVal = panelSheet.getRange("F5").getValue().toString().trim();
  if (cellVal.indexOf("pub_") === 0) {
    publishId = cellVal;
  }
  
  if (!publishId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún ID de publicación activo para cancelar.");
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
    var response = UrlFetchApp.fetch(backendUrl + "/publish/" + publishId + "/cancel", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      deleteTriggersForType_("publish");
      setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Publicación cancelada por el usuario.");
      SpreadsheetApp.getUi().alert("La solicitud de cancelación de la publicación fue enviada al backend correctamente.");
    } else {
      SpreadsheetApp.getUi().alert("Error al cancelar la publicación (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Inicia la indexación manual de fuentes en segundo plano.
 */
function indexSources() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
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
    "force_refresh": false
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
    setPanelStatus_("Procesando...", null, "Iniciando indexación...", "Iniciando indexación...");
    
    var response = UrlFetchApp.fetch(backendUrl + "/sources/index", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var jobId = data.job_id || "";
      
      PropertiesService.getScriptProperties().setProperty("LAST_INDEX_JOB_ID", jobId);
      var polling = tryScheduleAutoStatusCheck_("index");
      
      var msg = "Indexación de fuentes iniciada en segundo plano.";
      var resumen = "Ejecución activa: indexación en segundo plano";
      if (!polling.ok) {
        msg = "Indexación iniciada correctamente. No se pudo activar el seguimiento automático. Usa 'Consultar estado' para actualizar manualmente.";
        resumen = "Seguimiento automático no disponible";
      }
      
      setPanelStatus_("Procesando...", jobId, msg, resumen);
      
      var alertMsg = "Indexación iniciada correctamente.\nJob ID: " + jobId;
      if (!polling.ok) {
        alertMsg += "\n\nAVISO: No se pudo programar el seguimiento automático (" + polling.error + ").\nPuede consultar el progreso con 'Consultar indexación'.";
      } else {
        alertMsg += "\n\nPuede consultar el progreso con 'Consultar indexación'.";
      }
      SpreadsheetApp.getUi().alert(alertMsg);
    } else {
      setPanelStatus_("Error", "", "HTTP " + resCode + ": " + resText, "Error al iniciar indexación.");
      SpreadsheetApp.getUi().alert("Error al indexar (HTTP " + resCode + "):\n" + resText);
    }
  } catch (e) {
    setPanelStatus_("Error", "", e.toString(), "Error de conexión.");
    SpreadsheetApp.getUi().alert("Error de conexión:\n" + e.toString());
  }
}

/**
 * Consulta el estado del job de indexación de fuentes indicado en PropertiesService o F5.
 */
function checkIndexStatus() {
  var panelSheet;
  try {
    panelSheet = getPanelSheet_();
  } catch (err) {
    SpreadsheetApp.getUi().alert("Error: " + err.message);
    return;
  }
  
  var jobId = PropertiesService.getScriptProperties().getProperty("LAST_INDEX_JOB_ID");
  var cellVal = panelSheet.getRange("F5").getValue().toString().trim();
  if (cellVal.indexOf("idx_") === 0) {
    jobId = cellVal;
  }
  
  if (!jobId) {
    SpreadsheetApp.getUi().alert("Error: No hay ningún Job ID de indexación registrado.");
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
      
      var friendlyStatus = getFriendlyStatus_(status, "Indexando...");
      var progressMsg = "Dominios: " + comp + "/" + total + " — URLs encontradas: " + urls + " — Errores: " + errs;
      
      if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Ejecución cancelada");
      } else {
        var resumenTxt = "Ejecución activa: indexación en segundo plano";
        if (friendlyStatus === "Completado") {
          resumenTxt = "Ejecución completada con éxito";
        } else if (friendlyStatus === "Error") {
          resumenTxt = "Ejecución fallida con error";
        }
        setPanelStatus_(friendlyStatus, null, progressMsg, resumenTxt);
      }
      
      SpreadsheetApp.getActiveSpreadsheet().toast("Estado de indexación actualizado: " + friendlyStatus, "Encuentro Noticias", 5);
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
    var response = UrlFetchApp.fetch(backendUrl + "/logs/delete-all", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Borrado de logs completado.";
      var deleted = data.deleted_count || 0;
      
      panelSheet.getRange("B10").setValue(Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), "yyyy-MM-dd HH:mm:ss"));
      panelSheet.getRange("B11").setValue("Logs eliminados: " + deleted + " filas.");
      
      SpreadsheetApp.getUi().alert("Borrado de logs completado.\n" + msg + "\n\nDetalle:\n- Filas eliminadas: " + deleted);
    } else {
      SpreadsheetApp.getUi().alert("Error al borrar logs (HTTP " + resCode + "):\n" + resText);
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
    var response = UrlFetchApp.fetch(backendUrl + "/descartes/delete-all", options);
    var resCode = response.getResponseCode();
    var resText = response.getContentText();
    
    if (resCode >= 200 && resCode < 300) {
      var data = JSON.parse(resText);
      var msg = data.message || "Borrado de descartes completado.";
      var deleted = data.deleted_count || 0;
      
      panelSheet.getRange("B10").setValue(Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), "yyyy-MM-dd HH:mm:ss"));
      panelSheet.getRange("B11").setValue("Descartes eliminados: " + deleted + " filas.");
      SpreadsheetApp.getUi().alert("Borrado de descartes completado.\n" + msg + "\n\nDetalle:\n- Filas eliminadas: " + deleted);
    } else {
      SpreadsheetApp.getUi().alert("Error al borrar descartes (HTTP " + resCode + "):\n" + resText);
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

/**
 * Programa un trigger para verificar el estado de forma automática.
 * type puede ser: "search", "publish", "index"
 */
function scheduleAutoStatusCheck_(type) {
  deleteTriggersForType_(type);
  var functionName = "";
  if (type === "search") functionName = "autoCheckSearchStatus_";
  else if (type === "publish") functionName = "autoCheckPublishStatus_";
  else if (type === "index") functionName = "autoCheckIndexStatus_";
  if (functionName) {
    ScriptApp.newTrigger(functionName).timeBased().everyMinutes(1).create();
  }
}

/**
 * Elimina todos los triggers asociados a un tipo de proceso.
 */
function deleteTriggersForType_(type) {
  try {
    var functionName = "";
    if (type === "search") functionName = "autoCheckSearchStatus_";
    else if (type === "publish") functionName = "autoCheckPublishStatus_";
    else if (type === "index") functionName = "autoCheckIndexStatus_";
    if (!functionName) return;
    var triggers = ScriptApp.getProjectTriggers();
    for (var i = 0; i < triggers.length; i++) {
      if (triggers[i].getHandlerFunction() === functionName) {
        ScriptApp.deleteTrigger(triggers[i]);
      }
    }
  } catch (e) {
    Logger.log("Error al borrar triggers para " + type + ": " + e.toString());
  }
}

function autoCheckSearchStatus_() {
  var runId = PropertiesService.getScriptProperties().getProperty("LAST_SEARCH_RUN_ID");
  if (!runId) { deleteTriggersForType_("search"); return; }
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  if (!backendUrl) { deleteTriggersForType_("search"); return; }
  if (backendUrl.slice(-1) === "/") backendUrl = backendUrl.slice(0, -1);
  var headers = {};
  if (adminToken) headers["X-Admin-Token"] = adminToken;
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/runs/" + runId, { "method": "get", "headers": headers, "muteHttpExceptions": true });
    if (response.getResponseCode() === 200) {
      var data = JSON.parse(response.getContentText());
      var status = data.status || "";
      var processed = data.books_processed || 0;
      var total = data.books_total || 0;
      var msg = data.message || "";
      var friendlyStatus = getFriendlyStatus_(status, "Procesando...");
      var progressMsg = "Procesados: " + processed + "/" + total + " — " + msg;
      if (friendlyStatus === "Completado") {
        setPanelStatus_("Completado", null, progressMsg, "Búsqueda finalizada");
        deleteTriggersForType_("search");
      } else if (friendlyStatus === "Error") {
        setPanelStatus_("Error", null, progressMsg, "Búsqueda finalizada con error");
        deleteTriggersForType_("search");
      } else if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Búsqueda cancelada");
        deleteTriggersForType_("search");
      } else {
        setPanelStatus_("Procesando...", null, progressMsg, "Ejecución activa: búsqueda en segundo plano");
      }
    }
  } catch (e) { /* Ignorar errores de conexión temporales */ }
}

function autoCheckPublishStatus_() {
  var publishId = PropertiesService.getScriptProperties().getProperty("LAST_PUBLISH_ID");
  if (!publishId) { deleteTriggersForType_("publish"); return; }
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  if (!backendUrl) { deleteTriggersForType_("publish"); return; }
  if (backendUrl.slice(-1) === "/") backendUrl = backendUrl.slice(0, -1);
  var headers = {};
  if (adminToken) headers["X-Admin-Token"] = adminToken;
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/publish/" + publishId + "/status", { "method": "get", "headers": headers, "muteHttpExceptions": true });
    if (response.getResponseCode() === 200) {
      var data = JSON.parse(response.getContentText());
      var status = data.status || "";
      var pubCount = data.published_count || 0;
      var errCount = data.errors_count || 0;
      var msg = data.message || "";
      var friendlyStatus = getFriendlyStatus_(status, "Procesando...");
      var progressMsg = "Publicadas: " + pubCount + ", Errores: " + errCount + " — " + msg;
      if (friendlyStatus === "Completado") {
        setPanelStatus_("Completado", null, progressMsg, "Publicación finalizada");
        deleteTriggersForType_("publish");
      } else if (friendlyStatus === "Error") {
        setPanelStatus_("Error", null, progressMsg, "Publicación finalizada con error");
        deleteTriggersForType_("publish");
      } else if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Publicación cancelada");
        deleteTriggersForType_("publish");
      } else {
        setPanelStatus_("Procesando...", null, progressMsg, "Ejecución activa: publicación en segundo plano");
      }
    }
  } catch (e) { /* Ignorar errores de conexión temporales */ }
}

function autoCheckIndexStatus_() {
  var jobId = PropertiesService.getScriptProperties().getProperty("LAST_INDEX_JOB_ID");
  if (!jobId) { deleteTriggersForType_("index"); return; }
  var backendUrl = getConfigValue("BACKEND_BASE_URL");
  var adminToken = getConfigValue("ADMIN_TOKEN");
  if (!backendUrl) { deleteTriggersForType_("index"); return; }
  if (backendUrl.slice(-1) === "/") backendUrl = backendUrl.slice(0, -1);
  var headers = {};
  if (adminToken) headers["X-Admin-Token"] = adminToken;
  try {
    var response = UrlFetchApp.fetch(backendUrl + "/sources/index/status?job_id=" + jobId, { "method": "get", "headers": headers, "muteHttpExceptions": true });
    if (response.getResponseCode() === 200) {
      var data = JSON.parse(response.getContentText());
      var status = data.status || "";
      var comp = data.domains_completed || 0;
      var total = data.domains_total || 0;
      var urls = data.urls_found || 0;
      var errs = (data.errors || []).length;
      var friendlyStatus = getFriendlyStatus_(status, "Indexando...");
      var progressMsg = "Dominios: " + comp + "/" + total + " — URLs: " + urls + " — Errores: " + errs;
      if (friendlyStatus === "Completado") {
        setPanelStatus_("Completado", null, progressMsg, "Indexación finalizada");
        deleteTriggersForType_("index");
      } else if (friendlyStatus === "Error") {
        setPanelStatus_("Error", null, progressMsg, "Indexación finalizada con error");
        deleteTriggersForType_("index");
      } else if (friendlyStatus === "Cancelado") {
        setPanelStatus_("Cancelado", null, "Proceso cancelado por el usuario.", "Indexación cancelada");
        deleteTriggersForType_("index");
      } else {
        setPanelStatus_("Procesando...", null, progressMsg, "Ejecución activa: indexación en segundo plano");
      }
    }
  } catch (e) { /* Ignorar errores de conexión temporales */ }
}

/**
 * Helper común que intenta programar el seguimiento automático sin propagar excepciones.
 */
function tryScheduleAutoStatusCheck_(type) {
  try {
    scheduleAutoStatusCheck_(type);
    return { ok: true };
  } catch (e) {
    Logger.log("Error de polling para " + type + ": " + e.toString());
    return { ok: false, error: e.toString() };
  }
}

/**
 * Función pública para forzar la solicitud de autorización del permiso de triggers.
 */
function autorizarSeguimientoAutomatico() {
  try {
    ScriptApp.getProjectTriggers();
    SpreadsheetApp.getUi().alert("Seguimiento automático autorizado correctamente.");
  } catch (e) {
    SpreadsheetApp.getUi().alert(
      "Error al autorizar el seguimiento automático:\n\n" + 
      e.toString() + 
      "\n\nAsegúrate de conceder los permisos solicitados por Google."
    );
  }
}
