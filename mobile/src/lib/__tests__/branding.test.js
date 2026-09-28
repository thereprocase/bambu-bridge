/* global __dirname, test, expect */
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../../..');
const read = p => fs.readFileSync(path.join(root,p),'utf8');
test('packaged identity stays update-compatible with the Gridline display name', () => {
  const expo = JSON.parse(read('app.json')).expo;
  expect(expo.name).toBe('Bambu Bridge');
  expect(expo.android.package).toBe('com.anonymous.bambubridgeapp');
  expect(expo.android.adaptiveIcon.backgroundColor).toBe('#0000A8');
  expect(read('android/app/src/main/res/values/strings.xml')).toContain('>Bambu Bridge<');
  expect(read('android/app/src/main/res/values/colors.xml')).toContain('#C6C6C6');
  expect(read('android/app/src/main/res/values-night/colors.xml')).toContain('#181818');
});
test('all launcher densities include regular, adaptive and themed variants', () => {
  for (const density of ['mdpi','hdpi','xhdpi','xxhdpi','xxxhdpi']) {
    for (const suffix of ['', '_round', '_foreground', '_background', '_monochrome']) {
      const data = fs.readFileSync(path.join(root,`android/app/src/main/res/mipmap-${density}/ic_launcher${suffix}.webp`));
      expect(data.subarray(0,4).toString()).toBe('RIFF');
    }
  }
  expect(read('android/app/src/main/java/com/thereprocase/bambubridge/viewing/PrintMonitorService.kt')).toContain('R.drawable.ic_bridge_notification');
});
