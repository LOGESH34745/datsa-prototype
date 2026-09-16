// DATSA Prototype — persistent Leaflet map controller.
//
// Why this exists: regenerating a whole Folium/Leaflet HTML page on every
// animation frame causes the map to visibly re-zoom/re-center each tick
// (looks like flicker / zoom-in-zoom-out) and makes the moving target hard
// to see. Instead, we create the Leaflet map ONCE and then only ever move
// the existing marker / update the existing polylines in place — exactly
// like the standalone HTML prototype does.

window.dash_clientside = Object.assign({}, window.dash_clientside, {
  datsa: {
    updateMap: function (waypointsData, trackData, sliderVal) {

      // ---- one-time map initialization ----
      if (!window.datsaMapInitialized) {
        window.datsaMap = L.map('map-div', { zoomControl: true }).setView([20.2961, 85.8245], 5);

        L.tileLayer(
          'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
          { attribution: 'Esri World Imagery', maxZoom: 19 }
        ).addTo(window.datsaMap);

        window.datsaWaypointMarkers = [];
        window.datsaPreviewLine = null;
        window.datsaTrackLine = null;
        window.datsaTargetMarker = null;
        window.datsaLastWaypointsKey = null;
        window.datsaMapInitialized = true;
      }

      const map = window.datsaMap;

      // ---- waypoints: markers + a light dashed preview line ----
      if (waypointsData && waypointsData.length) {
        const key = JSON.stringify(waypointsData.map(w => [w.lat, w.lon, w.alt, w.speed]));
        if (key !== window.datsaLastWaypointsKey) {
          window.datsaLastWaypointsKey = key;

          window.datsaWaypointMarkers.forEach(m => map.removeLayer(m));
          window.datsaWaypointMarkers = [];
          if (window.datsaPreviewLine) { map.removeLayer(window.datsaPreviewLine); window.datsaPreviewLine = null; }

          const latlngs = waypointsData.map(w => [w.lat, w.lon]);
          window.datsaPreviewLine = L.polyline(latlngs, {
            color: '#3ddc97', weight: 2, dashArray: '5,6', opacity: 0.55,
          }).addTo(map);

          waypointsData.forEach((w, i) => {
            const isFirst = i === 0, isLast = i === waypointsData.length - 1;
            const color = isFirst ? '#ffb454' : (isLast ? '#ff6b6b' : '#3ddc97');
            const marker = L.circleMarker([w.lat, w.lon], {
              radius: 6, color: '#0d1b1e', weight: 2, fillColor: color, fillOpacity: 1,
            }).bindTooltip(`WP${i + 1} · ${Math.round(w.alt)}m · ${Math.round(w.speed)}m/s`);
            marker.addTo(map);
            window.datsaWaypointMarkers.push(marker);
          });

          // only refit the view when the waypoint set actually changes,
          // never on every playback tick
          if (!trackData || !trackData.length) {
            map.fitBounds(latlngs, { padding: [40, 40] });
          }
        }
      }

      // ---- simulated track: smooth curved line + moving target marker ----
      if (trackData && trackData.length) {
        const trackLatLngs = trackData.map(p => [p.lat, p.lon]);

        if (!window.datsaTrackLine) {
          window.datsaTrackLine = L.polyline(trackLatLngs, {
            color: '#3ddc97', weight: 3, opacity: 0.9,
          }).addTo(map);
          map.fitBounds(trackLatLngs, { padding: [40, 40] });
        } else {
          window.datsaTrackLine.setLatLngs(trackLatLngs);
        }

        const idx = Math.max(0, Math.min(sliderVal || 0, trackData.length - 1));
        const pt = trackData[idx];

        if (!window.datsaTargetMarker) {
          const icon = L.divIcon({
            className: '',
            html: '<div style="font-size:22px; line-height:22px;">🛩️</div>',
            iconSize: [26, 26], iconAnchor: [13, 13],
          });
          window.datsaTargetMarker = L.marker([pt.lat, pt.lon], { icon, zIndexOffset: 1000 }).addTo(map);
        } else {
          window.datsaTargetMarker.setLatLng([pt.lat, pt.lon]);
        }
      } else {
        // no track yet (or it was just cleared) — remove stale layers
        if (window.datsaTrackLine) { map.removeLayer(window.datsaTrackLine); window.datsaTrackLine = null; }
        if (window.datsaTargetMarker) { map.removeLayer(window.datsaTargetMarker); window.datsaTargetMarker = null; }
      }

      return '';
    },

    // Advances play-slider entirely in the browser — no server round-trip
    // per animation frame. This is what keeps playback smooth even when
    // the server is far away / on a slow free-tier host, since only the
    // slider's own value changes here (the map redraw below is still a
    // clientside callback too, so the whole animation loop never leaves
    // the browser).
    advancePlayback: function (n_intervals, current_val, max_val, speed_mult, playing) {
      if (!playing) return [window.dash_clientside.no_update, window.dash_clientside.no_update];
      if (current_val === null || current_val === undefined) current_val = 0;
      const step = Math.max(1, parseInt(speed_mult, 10) || 1);
      const new_val = current_val + step;
      if (new_val >= max_val) {
        return [max_val, false]; // reached the end — stop playback
      }
      return [new_val, window.dash_clientside.no_update];
    },
  },
});
