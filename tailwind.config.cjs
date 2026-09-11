const themeConfig = require('./static/tailwind-config.js');

module.exports = {
  content: ['./templates/**/*.html', './static/**/*.js'],
  darkMode: 'class',
  theme: themeConfig.theme,
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/container-queries')],
};
