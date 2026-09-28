// Deterministic raster exports from repository-owned vectors.
// Requires rsvg-convert and ImageMagick. Runtime identifiers and signing stay unchanged.
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');
const root = path.resolve(__dirname,'..');
const image = n => path.join(root,'assets/images',n);
const res = n => path.join(root,'android/app/src/main/res',n);
const svg = n => path.join(root,'assets/brand',n);
const raster = (source,size,out) => execFileSync('rsvg-convert',['-w',String(size),'-h',String(size),'-o',out,source]);
const convert = (...args) => execFileSync('magick',args);
raster(svg('mark.svg'),1024,image('android-icon-foreground.png'));
raster(svg('monochrome.svg'),1024,image('android-icon-monochrome.png'));
convert('-size','1024x1024','xc:#0000A8',image('android-icon-background.png'));
convert(image('android-icon-background.png'),image('android-icon-foreground.png'),'-compose','over','-composite','-crop','640x640+192+192','+repage','-resize','1024x1024',image('icon.png'));
convert(image('icon.png'),image('icon-dark.png'));
convert(image('icon.png'),'-resize','64x64',image('favicon.png'));
convert(image('icon.png'),image('splash-icon.png'));
for(const [density,launcher,adaptive,splash] of [['mdpi',48,108,200],['hdpi',72,162,300],['xhdpi',96,216,400],['xxhdpi',144,324,600],['xxxhdpi',192,432,800]]) {
  for(const [name,source,size] of [['ic_launcher','icon.png',launcher],['ic_launcher_round','icon.png',launcher],['ic_launcher_foreground','android-icon-foreground.png',adaptive],['ic_launcher_background','android-icon-background.png',adaptive],['ic_launcher_monochrome','android-icon-monochrome.png',adaptive]]) {
    convert(image(source),'-resize',`${size}x${size}`,'-define','webp:lossless=true',res(`mipmap-${density}/${name}.webp`));
  }
  for(const theme of ['', 'night-']) {
    const out = res(`drawable-${theme}${density}/splashscreen_logo.png`);
    if(fs.existsSync(path.dirname(out))) convert(image('splash-icon.png'),'-resize',`${splash}x${splash}`,out);
  }
}
console.log('Gridline icon and splash exports generated for all packaged densities.');
